from core import *
import sys, subprocess, math, time, argparse, importlib.util, urllib.request, html, fcntl, signal
sys.path.insert(0,str(HERMES))
from hermes_cli import kanban_db as kb
from failure_accounting import classify_failure as classify_attempt_failure, record_failure, ensure_blocked_failure_records
from limits import validate_count

# Hermes 0.21 split connection and dispatch helpers into dedicated modules
# without re-exporting every legacy symbol from ``kanban_db``.  Keep the rest
# of the pipeline on one small compatibility surface while supporting both the
# older bundled layout and the current public module layout.
if not hasattr(kb, 'connect'):
 from hermes_cli.kanban_db_connect import connect as _kanban_connect
 kb.connect = _kanban_connect
if not hasattr(kb, 'dispatch_once'):
 from hermes_cli.kanban_db_dispatch import dispatch_once as _kanban_dispatch_once
 kb.dispatch_once = _kanban_dispatch_once

# --- Scaling orchestration policy (audit P1-1/P1-3) -------------------------
# Stage-specific batching: a 25-business read-ig batch made the model emit
# unparseable JSON twice and hard-blocked run-20260921-114211; 5 businesses is
# the largest LLM/browser reader batch proven good in an accepted run
# (find-ig 625s for 5 in run-20260922-012920). Deterministic stages keep the
# large batch. LLM evidence-resolver (non-browser) gets a moderate batch.
BATCH_SIZES={'find-ig':5,'read-ig':5,'read-reviews':5,'find-owner':5,'resolve-owner':10}
DEFAULT_BATCH=25
# Stage-aware runtime policy, justified from measured run history:
#   deterministic        read-site 44s@5, ~220s@25 projected -> keep 900
#   reader_browser_llm   find-ig 625s@5 (2.9x margin)        -> 1800
#   discovery            find-web 2040s@60 in run-114211      -> 2700
#   llm_resolve          query-plan 15s; resolve-owner w/ 10-biz evidence -> 1200
TASK_CLASSES={'deterministic':900,'reader_browser_llm':1800,'discovery':2700,'llm_resolve':1200}
STALE_MARGIN=600
def stale_timeout():return max(TASK_CLASSES.values())+STALE_MARGIN
ROLE_CLASS={'intake':'deterministic','dedupe':'deterministic','read-google':'deterministic','read-site':'deterministic','resolve-ig':'deterministic','resolve-wa':'deterministic','resolve-dm':'deterministic','validator':'deterministic','consolidator':'deterministic','supervisor':'deterministic','render-md':'deterministic','render-xlsx':'deterministic','render-pdf':'deterministic','find-maps':'discovery','find-web':'discovery','find-ig':'reader_browser_llm','read-ig':'reader_browser_llm','read-reviews':'reader_browser_llm','find-owner':'reader_browser_llm','query-plan':'llm_resolve','resolve-owner':'llm_resolve'}
MAX_EXEC_ATTEMPTS=2      # semantic task-execution budget (unchanged)
INFRA_LIMIT=4            # bounded infrastructure-start budget per task
INFRA_START_TRIES=3      # browser-start retries inside one spawn call
INFRA_BACKOFF=[2,5,10]
# --- Bounded multi-lane dispatch (performance forensic audit P1) ------------
# Worker lanes are per-ROLE: every role owns a dedicated Chrome on its own
# CDP port with its own user-data-dir, so two tasks of DIFFERENT roles can
# never contend for a browser, while two tasks of the SAME role would. The
# dispatcher therefore runs at most K tasks system-wide but never more than
# one per role/profile (max_in_progress_per_profile=1 is a hard invariant).
# Serial rollback/control mode: LEADGEN_MAX_CONCURRENCY=1.
MAX_CONCURRENCY_DEFAULT=3
MAX_CONCURRENCY_CAP=4   # conservative safe cap until a 60-business benchmark
CONCURRENCY_ENV='LEADGEN_MAX_CONCURRENCY'
def configured_concurrency():
 v=os.environ.get(CONCURRENCY_ENV)
 if v is None or not str(v).strip():return MAX_CONCURRENCY_DEFAULT
 try:n=int(str(v).strip())
 except ValueError:raise ValueError(CONCURRENCY_ENV+' must be an integer between 1 and '+str(MAX_CONCURRENCY_CAP)+'; got '+str(v))
 if n<1 or n>MAX_CONCURRENCY_CAP:raise ValueError(CONCURRENCY_ENV+' must be an integer between 1 and '+str(MAX_CONCURRENCY_CAP)+'; got '+str(n))
 return n
def role_class(role):return ROLE_CLASS.get(role,'deterministic')
def concurrency_envelope():
 # A lane is viable when its role Chrome port is free now OR held by OUR OWN
 # dedicated instance for that role (a stale instance from an interrupted
 # run that chrome.start will terminate and reclaim). A port held by any
 # foreign process is never usable. K lanes need K viable ports; if fewer
 # exist the run must fail closed — never silently downgrade to fewer lanes.
 k=configured_concurrency()
 from execution import backend
 if backend()=='docker':return k,list(range(k))
 from chrome import port_free,own_instance_pids,role_port
 available=[role_port(r) for r in sorted(READERS)
            if port_free(role_port(r)) or own_instance_pids(ROOT/'browser'/r)]
 return k,available
def batch_size(role):return BATCH_SIZES.get(role,DEFAULT_BATCH)
def batch_slices(count,size):return [(i,min(i+size,count)) for i in range(0,count,size)]
def overlaps(a,b):return a[0]<b[1] and b[0]<a[1]
def classify_failure(exc):
 msg=str(exc)
 if isinstance(exc,json.JSONDecodeError) or isinstance(exc,jsonschema.ValidationError):return 'LLM_OUTPUT_INVALID'
 if msg.startswith(('Semantic extraction incomplete','Discovery under-delivered','Fresh discovery did not verify')):return 'SEMANTIC_FAILURE'
 if msg.startswith('Validation rejected'):return 'VALIDATION_FAILURE'
 if msg.startswith(('Dedicated browser port','Dedicated Chrome')):return 'BROWSER_START_FAILURE'
 if msg.startswith('Infrastructure start limit'):return 'INFRA_START_FAILURE'
 return 'TASK_EXECUTION_FAILURE'
def record_metric(run,record):
 p=Path(run)/'reports/run-metrics.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
 with p.open('a') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
 p.chmod(0o600)
def merge_candidates(candidates):
 # Deterministic name-key merge; identical to the pre-scaling dedupe rule
 # (strength unchanged). Business IDs are content-derived, so a business
 # rediscovered in a later round merges into the same identity.
 by={}
 for b in candidates:
  k=re.sub(r'\W+','',b['name_raw'].casefold())
  if k not in by:by[k]=b
  else:by[k]['evidence_ids']=sorted(set(by[k]['evidence_ids']+b['evidence_ids']))
 return by
def dedupe_select(candidates,spec,targets):
 by=merge_candidates(candidates)
 if targets:
  wanted={b['business_id'] for b in targets};pinned={b['business_id']:b for b in by.values() if b['business_id'] in wanted}
  if set(pinned)!=wanted:raise ValueError('Fresh discovery did not verify every repeat-test identity')
  return list(pinned.values())
 if len(by)<spec['count']:raise ValueError(f'Discovery under-delivered: requested {spec["count"]}, discovered {len(by)} after the bounded 2-round re-discovery; inspect raw/find-maps and raw/find-web outputs')
 return list(by.values())[:spec['count']]

def configure(run):
 run=Path(run);os.environ['HERMES_KANBAN_DB']=str(run/'control/kanban.db')
 os.environ['HERMES_KANBAN_WORKSPACES_ROOT']=str(run/'workspaces')
 os.environ['HERMES_KANBAN_BOARD']='leadgen'
 from execution import backend
 config_home=run/'control/docker-hermes'
 # The marker persists with the run so later status/resume commands cannot
 # accidentally reopen a Docker board in WAL mode when the caller omits the
 # backend environment variable.
 if backend()!='docker' and not config_home.exists():return kb.connect(run/'control/kanban.db')
 # Docker Desktop bind mounts cross a VM boundary where SQLite WAL shared
 # memory is unsafe.  Use Hermes' supported rollback journal setting for the
 # per-run board; no user configuration or credentials are mounted.
 config_home.mkdir(parents=True,exist_ok=True)
 config=(config_home/'config.yaml')
 if not config.exists():config.write_text('database:\n  journal_mode: delete\n')
 previous=os.environ.get('HERMES_HOME');os.environ['HERMES_HOME']=str(config_home)
 try:return kb.connect(run/'control/kanban.db')
 finally:
  if previous is None:os.environ.pop('HERMES_HOME',None)
  else:os.environ['HERMES_HOME']=previous
def preflight(spec,browser_check=True,execution_details=None):
 validate_count(spec.get('count'))
 check('run',spec);errors=[]
 from execution import backend,runtime_details
 try:execution_details=execution_details or runtime_details()
 except RuntimeError as exc:execution_details=None;errors.append(str(exc))
 if not set(spec['sources']) & {'google_maps','web'}:errors.append('Discovery needs google_maps or web')
 if backend()=='local' and not Path('/usr/bin/sandbox-exec').is_file():errors.append('Required macOS sandbox unavailable')
 if spec['country'] not in phonenumbers.SUPPORTED_REGIONS:errors.append('Unknown country code')
 for role,p in PROFILES.items():
  if set(p['tools'])-{'leadgen_open','leadgen_scroll','leadgen_link'}:errors.append('Unreviewed role tool '+role)
  if not profile_charter(role).is_file():errors.append('Missing pinned charter '+role)
  config=profile_charter(role).parents[2]/'config.yaml'
  import yaml
  c=yaml.safe_load(config.read_text())
  verify_cloud_model(c['model'])
  if c['model']!=p['model']:errors.append('Profile/manifest model mismatch '+role)
  if c.get('fallback_providers'):errors.append('Model fallback forbidden '+role)
  if c.get('toolsets')!=[] or c.get('platform_toolsets',{}).get('cli')!=[]:errors.append('Unsafe profile tools '+role)
  if c['kanban'].get('auto_decompose') is not False:errors.append('auto_decompose must be false')
  if c['browser'].get('use_real_profile') is not False:errors.append('Normal browser profile forbidden')
  if c['browser'].get('backend')!='local':errors.append('Only local Chrome is permitted '+role)
  if c.get('tools',{}).get('tool_search',{}).get('enabled')!='off':errors.append('Dynamic tool search must be disabled '+role)
  if c.get('mcp_servers'):errors.append('Unreviewed MCP tools forbidden '+role)
 for schema_path in (ROOT/'schemas').glob('*.json'):
  jsonschema.Draft202012Validator.check_schema(read(schema_path))
 for name in ['jsonschema','phonenumbers']+(['openpyxl'] if 'xlsx' in spec['formats'] else [])+(['reportlab','pypdf','pdfplumber','pypdfium2'] if 'pdf' in spec['formats'] else []):
  if not importlib.util.find_spec(name):errors.append('Missing dependency '+name)
 for fmt in spec['formats']:
  p=PROFILES['render-'+fmt]
  if fmt!='md' and not (profile_charter('render-'+fmt).parents[1]/fmt/'SKILL.md').is_file():errors.append('Missing renderer skill '+fmt)
  if fmt!='md' and not (profile_charter('render-'+fmt).parents[1]/fmt/'scripts'/(fmt+'_create.py')).is_file():errors.append('Missing bundled renderer helper '+fmt)
 if backend()=='local':
  if not Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome').exists():errors.append('Chrome missing')
  from tools.browser_tool_install import _find_agent_browser
  try:_find_agent_browser()
  except Exception as e:errors.append('Browser unavailable: '+str(e))
 try:
  req=urllib.request.Request('http://127.0.0.1:11434/api/tags')
  urllib.request.urlopen(req,timeout=5).close() # Existing local model service only.
 except Exception as e:errors.append('Configured local LLM endpoint unavailable: '+str(e))
 if not os.access(ROOT/'runs',os.W_OK):errors.append('Runs directory not writable')
 # Concurrency envelope (audit P1): verify the planned worker-lane count is
 # actually viable before dispatch — a lane needs its own role Chrome port.
 k,available=concurrency_envelope()
 if len(available)<k:
  from chrome import role_port
  errors.append(f'Concurrency envelope unavailable: only {len(available)} of {k} role browser ports usable (ports {[role_port(r) for r in sorted(READERS)]})')
 if errors:raise RuntimeError('Preflight failed: '+'; '.join(errors))
 if browser_check and backend()=='local':
  from chrome import start,stop,role_port
  probe_root=ROOT/'browser/preflight';(probe_root/'reports').mkdir(parents=True,exist_ok=True)
  browser_process=start(probe_root,'find-maps')
  stop(browser_process,port=role_port('find-maps'))
 return {'passed':True,'at':now(),'execution':execution_details,'configured_concurrency':k,'configured_per_profile_concurrency':1,'effective_concurrency':k,'max_total':MAX_CONCURRENCY_CAP,'per_browser':1,'available_role_ports':sorted(available),'failure_limit':2,'research_apis':False,'dm_probe':False,'skills':spec['formats'],'libreoffice_required':False}

def make_run(spec,repeat_from=None,selected_backend=None):
 from execution import backend,runtime_details
 selected=backend(selected_backend);details=runtime_details(selected)
 details['limits']=dict(details.get('limits',{}),semantic_attempts=MAX_EXEC_ATTEMPTS,infrastructure_attempts=INFRA_LIMIT,max_concurrency=configured_concurrency(),stage_timeouts_seconds=dict(TASK_CLASSES))
 pre=preflight(spec);pre['execution']=details
 targets=None
 if repeat_from:
  previous=read(Path(repeat_from)/'canonical/leads.json');check('canonical',previous)
  targets=[{k:b[k] for k in ['business_id','name_raw','url']} for b in previous['businesses']]
  if len(targets)!=spec['count']:raise ValueError('Repeat target count must equal run count')
 run=ROOT/'runs'/('run-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
 run.mkdir(mode=0o700)
 for d in ['control','raw','resolved','canonical','deliverables','reports','reports/incomplete','workspaces','audit']: (run/d).mkdir()
 (run/'reports/failures').mkdir() # Sandboxed workers cannot mkdir; failure reports must land here.
 import yaml
 manifest=dict(spec);manifest['execution']=details
 (run/'run.yaml').write_text(yaml.safe_dump(manifest,sort_keys=False));write(run/'control/spec.json',spec);write(run/'reports/preflight.json',pre)
 if targets:write(run/'control/targets.json',targets)
 conn=configure(run);graph={}
 def add(key,role,parents=[],batch=None,slice=None):
  out=run/('resolved' if role.startswith('resolve-') else 'raw')/role/(key+'.json')
  (run/'workspaces'/role).mkdir(parents=True,exist_ok=True)
  limit=TASK_CLASSES[role_class(role)]
  tid=kb.create_task(conn,title=key,body=json.dumps({'role':role,'batch':batch,'slice':slice,'output':str(out)}),assignee='lg-'+role,parents=[graph[x]['id'] for x in parents],workspace_kind='dir',workspace_path=str(run/'workspaces'/role),skills=[profile_charter(role).parent.name],max_retries=2,max_runtime_seconds=limit,created_by='leadgen',idempotency_key=str(run)+key)
  graph[key]={'id':tid,'role':role,'parents':parents,'batch':batch,'slice':slice,'task_class':role_class(role),'timeout_seconds':limit,'output':str(out)}
 add('intake','intake');add('query-plan','query-plan',['intake'])
 discoveries=[]
 for role,source in [('find-maps','google_maps'),('find-web','web')]:
  if source in spec['sources']:add(role,role,['query-plan']);discoveries.append(role)
 if not discoveries:raise ValueError('Discovery requires google_maps or web source')
 # Bounded re-discovery: round 1 merges discovery and reports sufficiency; a
 # second discovery round runs only when round 1 fell short (it short-circuits
 # to an empty pass otherwise). The final dedupe re-merges both rounds and
 # fails early with an explicit reason if the count still cannot be met.
 add('dedupe-1','dedupe',discoveries)
 r2=[]
 for role,source in [('find-maps','google_maps'),('find-web','web')]:
  if source in spec['sources']:add(role+'-r2',role,['query-plan','dedupe-1']);r2.append(role+'-r2')
 add('dedupe','dedupe',['dedupe-1']+r2)
 tails=[]
 READ_DEPS={'read-site':['read-google'],'find-ig':['read-site'],'read-ig':['find-ig'],'read-reviews':['read-google'],'find-owner':['read-site']}
 RESOLVE_DEPS={'resolve-ig':['read-site','find-ig','read-ig'],'resolve-wa':['read-google','read-site','read-ig'],'resolve-owner':['read-site','find-owner'],'resolve-dm':['read-reviews','read-ig']}
 # Stage-specific batching with explicit business slices: every task records
 # exactly which slice of the deduped business list it owns, and cross-role
 # parents are wired by slice overlap, so each business is processed exactly
 # once per stage and downstream tasks fan in from every covering batch.
 plan={}
 for role in ['read-google','read-site','find-ig','read-ig','read-reviews','find-owner']:
  if role in ['read-google','read-reviews'] and 'google_maps' not in spec['sources']:continue
  if role in ['find-ig','read-ig'] and 'instagram' not in spec['sources']:continue
  if role in ['read-site','find-owner'] and 'web' not in spec['sources']:continue
  plan[role]=batch_slices(spec['count'],batch_size(role))
 for role,slices in plan.items():
  for n,sl in enumerate(slices):
   key=role+f'-batch-{n+1:02d}';parents=['dedupe']
   for parent_role in READ_DEPS.get(role,[]):
    parents+=[f'{parent_role}-batch-{j+1:02d}' for j,psl in enumerate(plan.get(parent_role,[])) if overlaps(sl,psl) and f'{parent_role}-batch-{j+1:02d}' in graph]
   add(key,role,parents,n,sl);tails.append(key)
 for role,deps in RESOLVE_DEPS.items():
  for n,sl in enumerate(batch_slices(spec['count'],batch_size(role))):
   key=role+f'-batch-{n+1:02d}';parents=['dedupe']
   for parent_role in deps:
    parents+=[f'{parent_role}-batch-{j+1:02d}' for j,psl in enumerate(plan.get(parent_role,[])) if overlaps(sl,psl) and f'{parent_role}-batch-{j+1:02d}' in graph]
   add(key,role,parents,n,sl);tails.append(key)
 add('validator','validator',['dedupe']+tails)
 add('consolidator','consolidator',['validator'])
 add('supervisor','supervisor',['consolidator'])
 for fmt in spec['formats']:add('render-'+fmt,'render-'+fmt,['supervisor'])
 write(run/'control/graph.json',graph)
 audit(run,'supervisor','created',{'tasks':len(graph),'spec':spec})
 conn.close();return run

def all_inputs(run,graph,slice=None,keys=None):
 ev=[];res=[];businesses=[];queries=[]
 for key,node in graph.items():
  if keys is not None and key not in keys:continue
  p=Path(node['output'])
  if not p.exists():continue
  data=read(p)
  ev+=data.get('evidence',[]);res+=data.get('resolutions',[])
  if key=='dedupe':businesses=data['businesses']
  if key=='query-plan':queries=data['queries']
 if slice is not None:
  businesses=businesses[slice[0]:slice[1]];ids={b['business_id'] for b in businesses}
  ev=[e for e in ev if e['business_id'] in ids];res=[r for r in res if r['business_id'] in ids]
 return {'businesses':businesses,'evidence':ev,'resolutions':res,'queries':queries}

def deterministic_resolutions(inputs,spec):
 ev=inputs['evidence'];out=[]
 for b in inputs['businesses']:
  bid=b['business_id']
  for f in ['business_name','google_rating','google_review_count','dm_measured']:
   if f not in spec['requested_fields']:continue
   es=[e for e in ev if e['business_id']==bid and e['field']==f]
   # Prefer evidence that actually confirms the discovered name verbatim.
   # read-google/read-site may independently emit business_name evidence (a
   # Maps heading, a site logo alt text) using a shorter/different variant
   # of the same business' name (e.g. "Therapy Day Spa" vs the fuller
   # "Therapy Day Spa Pererenan" that find-web/find-maps actually discovered
   # and set as name_raw). Citing that non-matching evidence while resolving
   # to name_raw would fail provenance ("value absent from evidence") even
   # though the name is correct -- fall back to the original discovery
   # evidence, which by construction contains name_raw verbatim.
   if f=='business_name' and not any(b['name_raw'].casefold() in e['snippet'].casefold() for e in es):es=[e for e in ev if e['evidence_id'] in b['evidence_ids']]
   if f.startswith('google_'):es=[e for e in es if e['reader']=='read-google' and e['source_kind']=='google_maps']
   value=None;status='not_found';refs=[];method='Strict local parser from Maps UI; no inference'
   if f=='dm_measured':status='not_public';method='Probe disabled; no outreach performed'
   elif f=='business_name':
    refs=[e['evidence_id'] for e in es];value=b['name_raw'];status='found';method='Browser-discovered business name; identity retained'
   else:
    vals=[]
    for e in es:
     text=e['snippet']
     if f=='google_rating':
      matches=re.findall(r'(?<!\d)([0-5][.,]\d)(?!\d)',text,re.I)
      if not matches and re.fullmatch(r'[0-5][.,]\d',text.strip()):matches=[text.strip()]
      parsed=[float(x.replace(',','.')) for x in matches]
     else:
      matches=re.findall(r'([\d][\d,.\s]*)\s*(?:reviews?|ulasan)',text,re.I)
      if not matches and re.fullmatch(r'\(?[\d,. ]+\)?',text.strip()):matches=[text.strip().strip('()')]
      if not matches:matches=re.findall(r'\(([\d][\d,.\s]*)\)',text)
      parsed=[int(re.sub(r'\D','',x)) for x in matches if re.fullmatch(r'\d{1,3}(?:[,. ]\d{3})*|\d+',x.strip())]
     for v in parsed:vals.append((v,e['evidence_id']))
    if vals:
     refs=[x[1] for x in vals];unique={x[0] for x in vals}
     if len(unique)==1:value=vals[0][0];status='found'
     else:status='ambiguous'
    elif es:
     refs=[e['evidence_id'] for e in es]
     restricted=any(re.search(r'limited view|tampilan terbatas|login|log in|sign in|captcha',e['snippet'],re.I) for e in es)
     status='access_restricted' if restricted else 'error';method='Source access restriction prevented verification' if restricted else 'UI evidence could not be parsed unambiguously'
   out.append(resolution(bid,f,status,value,refs,ev,method,0.95 if status=='found' else 0))
 return out

def validate_all(run,inputs,spec):
 errors=[]
 for e in inputs['evidence']:
  try:verify_evidence(run,e)
  except Exception as exc:errors.append('Evidence '+e.get('evidence_id','?')+': '+str(exc))
 rows=inputs['resolutions']+deterministic_resolutions(inputs,spec)
 seen=set()
 for r in rows:
  try:
   validate_resolution(r,inputs['evidence'],spec['country'])
   from quality import validate_instagram_identity
   validate_instagram_identity(r,inputs['evidence'])
   key=(r['business_id'],r['field'])
   if key in seen:raise ValueError('Duplicate resolved field')
   seen.add(key)
  except Exception as exc:errors.append('Resolution: '+str(exc))
 expected={(b['business_id'],f) for b in inputs['businesses'] for f in spec['requested_fields']}
 if seen!=expected:errors.append('Missing or unauthorized canonical fields: '+str(expected.symmetric_difference(seen)))
 v={'valid':not errors,'errors':errors,'checked_at':now()};check('validation',v)
 write(Path(run)/'raw/validator/rejects.json',errors)
 write(Path(run)/'raw/validator/validation.json',v)
 if errors:raise ValueError('Validation rejected: '+'; '.join(errors)[:1800])
 return {'validation':v,'resolutions':rows}

def consolidate(run,inputs,spec):
 v=read(Path(run)/'raw/validator/validator.json')
 if not v['validation']['valid']:raise ValueError('Validation not passed')
 dataset={'run_id':Path(run).name,'spec':spec,'businesses':inputs['businesses'],'evidence':inputs['evidence'],'resolved_fields':v['resolutions'],'validation':v['validation'],'source_priority':PRIORITY,'approved':False}
 check('canonical',dataset)
 write(Path(run)/'canonical/leads.json',dataset,immutable=True)
 write(Path(run)/'canonical/rejects.json',[],immutable=True)
 # CSV is an export, never the authoritative datastore.
 with (Path(run)/'canonical/leads.csv').open('w') as f:
  writer=csv.writer(f);writer.writerow(['business_id','field','value','status','confidence','evidence_ids'])
  for r in v['resolutions']:
   value=json.dumps(r['value'],ensure_ascii=False) if isinstance(r['value'],dict) else str(r['value'] or '')
   if value.startswith(('=','+','-','@')):value="'"+value
   writer.writerow([r['business_id'],r['field'],value,r['status'],r['confidence'],';'.join(r['evidence_ids'])])
 return {'canonical_sha256':digest(dataset),'business_count':len(dataset['businesses'])}

def semantic_report(run,data,spec):
 # Cross-references raw evidence presence against resolved status/value, so a
 # task status of 'done' is never mistaken for proof that a field was actually
 # extracted. Purely informational; never changes acceptance gating.
 ev=data['evidence'];by_business={}
 for e in ev:by_business.setdefault(e['business_id'],[]).append(e)
 rows=[]
 for b in data['businesses']:
  bid=b['business_id'];bev=by_business.get(bid,[])
  maps_rating_seen=any(e['reader']=='read-google' and e['source_kind']=='google_maps' and e['field']=='google_rating' for e in bev)
  maps_review_seen=any(e['reader']=='read-google' and e['source_kind']=='google_maps' and e['field']=='google_review_count' for e in bev)
  ig_evidence_seen=any(e['field'] in ('instagram','instagram_candidate') for e in bev)
  wa_numeric_seen=any(e['field'] in ('whatsapp','public_contact') and contact_values(e,'whatsapp',spec['country']) for e in bev)
  public_numeric_seen=any(e['field']=='public_contact' and contact_values(e,'public_contact',spec['country']) for e in bev)
  fields={r['field']:{'status':r['status'],'value':r['value']} for r in data['resolved_fields'] if r['business_id']==bid}
  rows.append({'business_id':bid,'business_name':b['name_raw'],
   'google_rating':dict(fields.get('google_rating',{}),maps_evidence_observed=maps_rating_seen),
   'google_review_count':dict(fields.get('google_review_count',{}),maps_evidence_observed=maps_review_seen),
   'instagram':dict(fields.get('instagram',{}),candidate_evidence_observed=ig_evidence_seen),
   'whatsapp':dict(fields.get('whatsapp',{}),numeric_contact_evidence_observed=wa_numeric_seen),
   'public_contact':dict(fields.get('public_contact',{}),numeric_contact_evidence_observed=public_numeric_seen),
   'owner':fields.get('owner',{})})
 report={'run_id':data['run_id'],'generated_at':now(),'note':"Task status 'done' does not by itself imply a field was successfully extracted; this cross-references raw evidence presence against resolved status.",'businesses':rows}
 report['reader_coverage']=[{'reader':role,'business_id':b['business_id'],'expected_fields':sorted(ALLOWED[role]),'observed_fields':sorted({e['field'] for e in ev if e['reader']==role and e['business_id']==b['business_id']}),'missing_fields':sorted(ALLOWED[role]-{e['field'] for e in ev if e['reader']==role and e['business_id']==b['business_id']})} for role in sorted(READERS-{'find-maps','find-web'}) for b in data['businesses']]
 report['field_coverage']={f:{'expected':len(data['businesses']),'found':sum(r['field']==f and r['status']=='found' for r in data['resolved_fields'])} for f in spec.get('requested_fields',sorted({r['field'] for r in data['resolved_fields']}))}
 write(Path(run)/'reports/semantic-completion.json',report)
 return report

ACCEPTANCE_POLICY=ROOT/'acceptance-policy.json'
ITERATION_EXHAUSTED_MARKERS=('Iteration budget exhausted','Reached maximum iterations')

def load_acceptance_policy():
 policy=read(ACCEPTANCE_POLICY)
 required={'version','gate_fields','soft_fields','informational_fields','minimum_resolved_percent','overall_useful_cell_ratio'}
 if set(policy)!=required:raise ValueError('Invalid acceptance policy keys')
 if not 0<=policy['minimum_resolved_percent']<=100:raise ValueError('Invalid acceptance coverage threshold')
 return policy

def acceptance_coverage(data,spec):
 fields=spec.get('requested_fields',sorted({r['field'] for r in data['resolved_fields']}))
 result={};all_rows=[]
 for field in fields:
  rows=[r for r in data['resolved_fields'] if r['field']==field];all_rows+=rows
  expected=len(data['businesses']);counts={s:sum(r['status']==s for r in rows) for s in ['found','not_found','not_public','ambiguous','access_restricted','error']}
  pct=lambda n:round((100*n/expected),1) if expected else 0.0
  result[field]={'expected':expected,**counts,'found_percent':pct(counts['found']),'resolved_percent':pct(counts['found']+counts['not_public']),'failed_percent':pct(counts['not_found']+counts['ambiguous']+counts['access_restricted']+counts['error']),'access_restricted_percent':pct(counts['access_restricted'])}
 useful=sum(r['status'] in {'found','not_public'} for r in all_rows)
 overall=round(100*useful/len(all_rows),1) if all_rows else 0.0
 return result,overall

def record_iteration_exhaustion(run,key,role):
 run=Path(run);log=run/'reports'/(key+'.log')
 try:text=log.read_text(errors='replace')
 except OSError:return False
 if not any(marker in text for marker in ITERATION_EXHAUSTED_MARKERS):return False
 item={'task':key,'role':role,'reason':'iteration_exhausted','at':now(),'log':str(log)}
 write(run/'reports/incomplete'/(key+'.json'),item)
 audit(run,role,'iteration_exhausted',item)
 return True

def iteration_exhausted_tasks(run):
 return sorted(p.stem for p in (Path(run)/'reports/incomplete').glob('*.json')) if (Path(run)/'reports/incomplete').is_dir() else []

def write_run_report(run,state,data,approval):
 execution=approval.get('execution',{})
 lines=['# Run report','',f"State: {state}",'',f"Businesses: {len(data['businesses'])}",'',f"Execution backend: {execution.get('backend','unknown')}",f"Image: {execution.get('image') or 'n/a'}",f"Image digest: {execution.get('image_digest') or 'n/a'}",f"Effective limits: {json.dumps(execution.get('limits',{}),sort_keys=True)}",'',f"Overall useful-cell ratio: {approval['overall_useful_cell_percent']}%",'', '## Field coverage','']
 for field,c in approval['field_coverage'].items():
  lines.append(f"- **{field}** — Found: {c['found_percent']}%; Resolved: {c['resolved_percent']}%; Failed: {c['failed_percent']}%; Access restricted: {c['access_restricted_percent']}%")
 if approval['gate_failures']:lines+=['','Gate failures: '+', '.join(approval['gate_failures'])]
 if approval['warnings']:lines+=['','Warnings: '+', '.join(approval['warnings'])]
 if approval['iteration_exhausted_tasks']:lines+=['','Iteration-exhausted tasks: '+', '.join(approval['iteration_exhausted_tasks'])]
 source_health_path=Path(run)/'reports/source-health.json';source_health=read(source_health_path) if source_health_path.exists() else {}
 lines+=['','## Source health','']
 for source,h in sorted(source_health.items()):lines.append(f"- **{source}** — status: {h.get('status','unknown')}; pages ok: {h.get('pages_ok',0)}; restricted: {h.get('pages_restricted',0)}; blocked: {str(bool(h.get('blocked'))).lower()}")
 if not source_health:lines.append('- No source-health observations recorded.')
 skipped_path=Path(run)/'reports/skipped-source-blocked.json';skipped=read(skipped_path) if skipped_path.exists() else []
 if skipped:
  lines+=['','## Skipped (source blocked)','']
  for s in skipped:
   fields=', '.join(s.get('fields_access_restricted') or []) or 'none (no resolution-owned field)'
   lines.append(f"- **{s['task']}** ({s['role']}) — skipped: source blocked [{s['source']}]; citing block snapshot {s.get('block_snapshot_id') or 'n/a'}; fields access_restricted: {fields}")
 lines+=['','DM responsiveness is public-evidence proxy only; measured DM is unavailable. No outreach.','',f"Canonical SHA256: {approval['canonical_sha256']}",'']
 (Path(run)/'reports/run-report.md').write_text('\n'.join(lines))

def gate(run,spec):
 run=Path(run);data=read(run/'canonical/leads.json');errors=[];policy=load_acceptance_policy()
 from source_health import merge_source_health
 merge_source_health(run) # publish role-staged source health before the final report
 if len(data['businesses'])!=spec['count']:errors.append(f"Requested {spec['count']} businesses; collected {len(data['businesses'])}")
 if any(r['status']=='error' for r in data['resolved_fields']):errors.append('Unresolved error fields')
 if not data['validation']['valid']:errors.append('Validation failed')
 field_coverage,overall=acceptance_coverage(data,spec);exhausted=iteration_exhausted_tasks(run)
 gate_failures=[f for f in policy['gate_fields'] if f in spec.get('requested_fields',[]) and field_coverage.get(f,{}).get('resolved_percent',0)<policy['minimum_resolved_percent']]
 warnings=[f+' below '+str(policy['minimum_resolved_percent'])+'% resolved' for f in policy['soft_fields'] if f in spec.get('requested_fields',[]) and field_coverage.get(f,{}).get('resolved_percent',0)<policy['minimum_resolved_percent']]
 state='failed' if errors else ('partial_success' if gate_failures or exhausted else 'approved')
 coverage={s:sum(r['status']==s for r in data['resolved_fields']) for s in ['found','not_found','not_public','ambiguous','access_restricted','error']}
 import yaml
 execution=yaml.safe_load((run/'run.yaml').read_text()).get('execution',{}) if (run/'run.yaml').exists() else {}
 approval={'state':state,'approved':state=='approved','canonical_sha256':digest(data),'at':now(),'coverage':coverage,'field_coverage':field_coverage,'overall_useful_cell_percent':overall,'gate_failures':gate_failures,'warnings':warnings,'iteration_exhausted_tasks':exhausted,'policy':policy,'errors':errors,'execution':execution}
 write(run/'reports/run-state.json',approval)
 write_run_report(run,state,data,approval)
 if errors:
  write(run/'control/approval.json',approval)
  raise ValueError('Acceptance gate: '+'; '.join(errors))
 write(run/'control/approval.json',approval,immutable=True)
 semantic_report(run,data,spec)
 return approval

# ── Host-level source gate ─────────────────────────────────────────────────
# Once source health records that a source has blocked this run (repeated
# access restrictions), the host must not spawn another task that would open a
# page on that source -- a worker would only rediscover the same login wall.
# The gate runs in run_loop before every dispatch, so it covers all batch and
# r2 variants of every role that opens Google Maps.
GATED_SOURCE_ROLES={'find-maps':'google_maps','read-google':'google_maps','read-reviews':'google_maps'}
# Fields a skipped Maps batch can honestly record as access_restricted: the
# canonical fields its evidence feeds. Discovery-only fields ('candidate') and
# evidence-only proxy signals are not resolution columns, so there is nothing
# to record for them -- they are left empty rather than invented.
RESTRICTABLE_FIELDS=frozenset({'google_rating','google_review_count','public_contact','website'})

def block_reference(run,source):
 '''The immutable snapshot that established the block for a source, if one was
 staged. Recorded by record_source_page so later businesses whose pages are
 never opened can still be cited honestly instead of guessed at.'''
 from source_health import load_source_health
 ref=(load_source_health(run).get(source) or {}).get('last_restriction') or {}
 return {'snapshot_id':ref['snapshot_id'],'line':ref.get('line') or ''} if ref.get('snapshot_id') else None

def skipped_source_output(run,role,businesses,source):
 '''Result for a task deliberately NOT spawned because its source is blocked.
 Everything the batch would have read stays honestly unresolved: the fields it
 owns are recorded access_restricted citing the snapshot that established the
 block, and nothing is claimed found, not_found or not_public.'''
 from core import ALLOWED
 from quality import evidence
 run=Path(run);ref=block_reference(run,source);ev=[]
 if ref and (run/'raw'/role/'snapshots'/(ref['snapshot_id']+'.json')).is_file():
  fields=sorted(f for f in ALLOWED.get(role,set()) if f in RESTRICTABLE_FIELDS)
  for b in businesses:
   for f in fields:
    ev.append(evidence(run,role,b['business_id'],f,{'snapshot_id':ref['snapshot_id']},ref['line'],source))
 return {'businesses':[],'evidence':ev,
  'skipped_source_blocked':{'source':source,'reason':'skipped: source blocked',
   'block_snapshot_id':ref['snapshot_id'] if ref else None,
   'fields_access_restricted':sorted({e['field'] for e in ev})}}

def skip_blocked_source_tasks(run,conn,graph):
 '''Host-level source gate: before the dispatcher spawns anything, complete
 without a worker every ready task whose role would open a blocked source.
 Returns the skip records (task, role, source, cited snapshot, restricted
 fields); the caller accumulates them for status.json and the run report.'''
 from source_health import load_source_health
 run=Path(run)
 blocked={s for s,h in load_source_health(run).items() if h.get('blocked')}
 if not any(GATED_SOURCE_ROLES.get(n['role']) in blocked for n in graph.values()):return []
 skipped=[]
 while True:
  # Re-run each pass: skipping a batch releases its children, which may
  # themselves be gated (e.g. read-reviews waits on read-google).
  kb.recompute_ready(conn) # promote parents-satisfied todo -> ready (dispatcher does the same)
  made=False
  for key,node in graph.items():
   source=GATED_SOURCE_ROLES.get(node['role'])
   if source not in blocked:continue
   row=conn.execute('SELECT status FROM tasks WHERE id=?',(node['id'],)).fetchone()
   if not row or row['status']!='ready':continue
   if kb.unsatisfied_parents(conn,node['id']):continue
   businesses=[]
   if node.get('slice'):
    try:
     bs=read(run/'raw/dedupe/dedupe.json')['businesses']
     businesses=[{'business_id':b['business_id']} for b in bs[node['slice'][0]:node['slice'][1]]]
    except Exception:businesses=[]
   out=skipped_source_output(run,node['role'],businesses,source)
   write(node['output'],out,immutable=True)
   kb.complete_task(conn,node['id'],result=json.dumps({'output':node['output'],'skipped_source_blocked':True}))
   record={'task':key,'role':node['role'],'source':source,'at':now(),
    'block_snapshot_id':out['skipped_source_blocked']['block_snapshot_id'],
    'fields_access_restricted':out['skipped_source_blocked']['fields_access_restricted']}
   record_metric(run,{'event':'skipped_source_blocked',**record})
   audit(run,'supervisor','skipped_source_blocked',record)
   skipped.append(record);made=True
  if not made:break
 if skipped:print('Skipped (source blocked):',[s['task'] for s in skipped],flush=True)
 return skipped

def render(run,fmt):
 data=read(Path(run)/'canonical/leads.json');approval=read(Path(run)/'control/approval.json')
 if approval.get('state','approved' if approval.get('approved') else 'failed') not in {'approved','partial_success'} or digest(data)!=approval['canonical_sha256']:raise ValueError('Completed acceptance decision missing or hash mismatch')
 skill=Path(os.environ.get('LEADGEN_CHARTER',profile_charter('render-'+fmt)))
 if not skill.is_file():raise ValueError('Pinned renderer skill missing')
 evidence={e['evidence_id']:e for e in data['evidence']}
 state=approval.get('state','approved' if approval.get('approved') else 'failed')
 coverage_lines=[f"{field}: found {values['found_percent']}%, resolved {values['resolved_percent']}%, failed {values['failed_percent']}%" for field,values in approval.get('field_coverage',{}).items()]
 banner=f"STATE: {state}"
 lines=['# '+banner,'','# Business research','',f"Run: {data['run_id']}",'','## Coverage summary','']+[f'- {line}' for line in coverage_lines]+['','DM proxy describes public evidence only. It does not measure message responsiveness. No outreach was performed.','']
 rows=[['Business','Field','Value','Status','Confidence','Sources']]
 for b in data['businesses']:
  lines+=['## '+b['name_raw'],'']
  for r in data['resolved_fields']:
   if r['business_id']!=b['business_id']:continue
   value=json.dumps(r['value'],ensure_ascii=False) if isinstance(r['value'],dict) else str(r['value'] if r['value'] is not None else 'Unavailable')
   urls=sorted({evidence[i]['source_url'] for i in r['evidence_ids']})
   safevalue=value.replace('<','&lt;').replace('>','&gt;')
   lines+=[f"- **{r['field']}**: {safevalue} — {r['status']} (confidence {r['confidence']})"+(' · '+', '.join(f'[source]({u})' for u in urls) if urls else '')]
   rows.append([b['name_raw'],r['field'],value,r['status'],r['confidence'],'\n'.join(urls)])
  lines+=['']
 out=Path(run)/'deliverables'/('leads.'+fmt)
 if fmt=='md':out.write_text('\n'.join(lines))
 else:
  bundled=Path(os.environ.get('LEADGEN_RENDER_BUNDLE',skill.parents[1]/fmt))
  if not (bundled/'SKILL.md').is_file():raise ValueError('Required bundled skill absent')
  if fmt=='xlsx':
   for row in rows:
    for i,val in enumerate(row):
     if isinstance(val,str) and val.startswith(('=','+','-','@')):row[i]="'"+val
   summary=[['Run state',state],['Overall useful-cell ratio',str(approval.get('overall_useful_cell_percent',0))+'%'],[],['Field','Found %','Resolved %','Failed %']]
   summary += [[field,values['found_percent'],values['resolved_percent'],values['failed_percent']] for field,values in approval.get('field_coverage',{}).items()]
   spec={'sheets':[{'name':'Summary','rows':summary,'column_widths':{'A':32,'B':18,'C':18,'D':18}},{'name':'Leads','rows':rows,'freeze_panes':'A2','column_widths':{'A':28,'B':24,'C':65,'D':16,'E':12,'F':75},'autofilter':f'A1:F{len(rows)}'}]}
   args=[str(bundled/'scripts/xlsx_create.py'),'-',str(out)]
  else:
   elements=[{'type':'heading','text':banner,'level':1},{'type':'heading','text':'Coverage summary','level':2}]+[{'type':'paragraph','text':line} for line in coverage_lines]+[{'type':'heading','text':'Business research','level':2},{'type':'paragraph','text':'Public activity proxy only; not measured DM responsiveness. No outreach performed.'}]
   for row in rows[1:]:
    elements += [{'type':'heading','text':html.escape(row[0]+' / '+row[1]),'level':3},{'type':'paragraph','text':html.escape(str(row[2])+' — '+row[3]+'; confidence '+str(row[4]))},{'type':'paragraph','text':html.escape(row[5])}]
   spec={'title':'Business research','elements':elements};sp=Path(run)/'deliverables/pdf-spec.json';write(sp,spec);args=[str(bundled/'scripts/pdf_create.py'),str(sp),'-o',str(out)]
  subprocess.run([sys.executable]+args,input=json.dumps(spec),text=True,check=True,capture_output=True)
 if not out.is_file() or not out.stat().st_size:raise ValueError('Renderer produced no file')
 return {'file':str(out),'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'canonical_sha256':digest(data),'skill':str(skill)}

def execute(run,key):
 run=Path(run);graph=read(run/'control/graph.json');spec=read(run/'control/spec.json');node=graph[key];role=node['role']
 ancestors=set()
 def visit(k):
  for parent in graph[k]['parents']:
   if parent not in ancestors:ancestors.add(parent);visit(parent)
 visit(key)
 inputs={} if role in {'supervisor','render-md','render-xlsx','render-pdf'} else all_inputs(run,graph,node.get('slice'),ancestors)
 if (Path(run)/'control/targets.json').exists():inputs['targets']=read(Path(run)/'control/targets.json')
 # Only dependency-approved stages are reachable through Kanban; file writes are fixed by role.
 if role=='intake':check('run',spec);out={'spec':spec}
 elif role=='query-plan':
  from worker import llm
  out=llm(run,role,'Create local-language and synonym public browser search queries for '+json.dumps(spec)+'\nRestrict discovery to these repeat-acceptance business identities if supplied: '+json.dumps(inputs.get('targets',[])),'query')
 elif role in READERS:
  from worker import reader
  if key.endswith('-r2') and read(Path(graph['dedupe-1']['output'])).get('sufficient'):
   audit(run,role,'rediscovery_skipped',{'task':key,'reason':'Discovery round 1 already satisfied the requested count'});out={'businesses':[],'evidence':[]}
  else:
   if key.endswith('-r2'):
    prior=read(Path(graph['dedupe-1']['output']))
    # Round-2 guidance is routing only: it never relaxes dedupe or identity checks.
    inputs['rediscovery']={'discovered':prior['discovered'],'requested':spec['count'],'names':[b['name_raw'] for b in prior['candidates']]}
   out=reader(run,role,inputs,spec)
 elif role=='dedupe' and key=='dedupe-1':
  candidates=[];evidence=[]
  for parent in node['parents']:
   d=read(graph[parent]['output']);candidates+=d['businesses'];evidence+=d['evidence']
  by=merge_candidates(candidates)
  wanted={b['business_id'] for b in inputs.get('targets',[])}
  sufficient=len(by)>=spec['count'] if not wanted else wanted<= {b['business_id'] for b in by.values()}
  out={'candidates':list(by.values()),'discovered':len(by),'sufficient':sufficient}
 elif role=='dedupe':
  candidates=list(read(Path(graph['dedupe-1']['output']))['candidates'])
  for parent in node['parents']:
   if parent=='dedupe-1':continue
   candidates+=read(graph[parent]['output']).get('businesses',[])
  out={'businesses':dedupe_select(candidates,spec,inputs.get('targets'))} # Evidence remains at its immutable original location.
 elif role.startswith('resolve-'):
  from worker import resolve
  out=resolve(run,role,inputs,spec)
  # Jev SHADOW observation only (experimental, flag-gated): records a parallel
  # judgment for calibration. It derives nothing, modifies nothing, and can
  # never raise; the pipeline result above remains the sole authority.
  if role in {'resolve-owner','resolve-dm'}:
   import jev_shadow
   jev_shadow.collect_stage(run,role,inputs,out)
 elif role=='validator':out=validate_all(run,inputs,spec)
 elif role=='consolidator':out=consolidate(run,inputs,spec)
 elif role=='supervisor':out=gate(run,spec)
 elif role.startswith('render-'):out=render(run,role.split('-')[1])
 else:raise PermissionError('Probe/out-of-scope role disabled')
 write(node['output'],out,immutable=True);audit(run,role,'completed',{'task':key,'output_sha256':digest(out)})
 return out

def worker_main(run,key):
 node=read(Path(run)/'control/graph.json')[key]
 if os.environ.get('LEADGEN_CONTAINER')=='1':
  try:
   execute(run,key)
  except Exception as e:
   category=classify_failure(e)
   failure_class=classify_attempt_failure(first_model_or_tool_call=True,message=str(e))
   record=record_failure(run,task=key,failure_class=failure_class,exit_code=1,stage=node['role'],reason=str(e)[:2000],failure_type='worker_exception')
   record.update({'role':node['role'],'category':category,'error':str(e)[:2000]});write(Path(run)/'reports/failures'/f'{key}.json',record)
   audit(run,node['role'],'failure',{'task':key,'category':category,'error':str(e)})
   raise
  return
 conn=configure(run)
 task=conn.execute('SELECT status,current_run_id FROM tasks WHERE id=?',(node['id'],)).fetchone()
 if task['status']!='running':raise ValueError('Task has not been claimed')
 try:
  # Bounded schema/transient retry: one reattempt; never repair rejected data silently.
  out=execute(run,key)
  kb.complete_task(conn,node['id'],result=json.dumps({'output':node['output']}),expected_run_id=task['current_run_id'])
 except Exception as e:
  category=classify_failure(e)
  failure_class=classify_attempt_failure(first_model_or_tool_call=True,message=str(e))
  record=record_failure(run,task=key,failure_class=failure_class,exit_code=1,stage=node['role'],reason=str(e)[:2000],failure_type='worker_exception')
  record.update({'role':node['role'],'category':category,'error':str(e)[:2000]});write(Path(run)/'reports/failures'/f'{key}.json',record)
  audit(run,node['role'],'failure',{'task':key,'category':category,'error':str(e)})
  kb.block_task(conn,node['id'],reason=f'[{category}] '+str(e)[:1900],kind='capability',expected_run_id=task['current_run_id'])
  raise
 finally:conn.close()

def stop_worker(proc):
 if proc.poll() is not None:return
 try:os.killpg(proc.pid,signal.SIGTERM)
 except ProcessLookupError:return
 try:proc.wait(timeout=5)
 except subprocess.TimeoutExpired:
  try:os.killpg(proc.pid,signal.SIGKILL)
  except ProcessLookupError:pass
  proc.wait()

def launch_worker(run,key,node,attempts,infra,attempts_path,infra_path):
 # Separate budgets: semantic task-execution attempts are consumed ONLY after
 # sandbox, dedicated browser and worker process have all started
 # successfully. Infrastructure startup failures draw from a separate bounded
 # budget (with in-call browser-start retries/backoff) and never poison the
 # task's two-attempt semantic limit.
 role=node['role'];tid=node['id']
 if attempts.get(tid,0)>=MAX_EXEC_ATTEMPTS:raise RuntimeError('Two-attempt limit reached; explicit repair required')
 if infra.get(tid,0)>=INFRA_LIMIT:raise RuntimeError('Infrastructure start limit reached; explicit repair required')
 from execution import backend,docker_command
 execution_backend=backend();browser_process=None
 if execution_backend=='local' and role in READERS:
  from chrome import start
  for try_n in range(INFRA_START_TRIES):
   try:browser_process=start(run,role);break
   except RuntimeError as exc:
    if try_n==INFRA_START_TRIES-1:
     infra[tid]=infra.get(tid,0)+1;write(infra_path,infra)
     audit(run,role,'infra_start_failure',{'task':key,'category':'BROWSER_START_FAILURE','infra_attempt':infra[tid],'error':str(exc)})
     raise
    audit(run,role,'infra_start_retry',{'task':key,'try':try_n+1,'backoff_seconds':INFRA_BACKOFF[try_n],'error':str(exc)[:300]});time.sleep(INFRA_BACKOFF[try_n])
 try:
  log_path=Path(run)/'reports'/(key+'.log')
  env={'PATH':os.environ['PATH'],'HOME':os.environ['HOME'],'LANG':'en_US.UTF-8','TMPDIR':os.environ.get('TMPDIR','/tmp'),'HERMES_HOME':str(profile_charter(role).parents[2]),'LEADGEN_EXECUTION_BACKEND':execution_backend}
  # Jev shadow flags/key pass through ONLY when the operator set them; the
  # default pipeline environment contains none of these.
  for k in ['JEV_SHADOW_ENABLED','JEV_SHADOW_OWNER','JEV_SHADOW_REVIEW_REPLY','JEV_SHADOW_DIR','TYPESAFE_API_KEY','JEV_AB_ENABLED','JEV_AB_OWNER','JEV_AB_MAPS','JEV_AB_OWNER_T','JEV_AB_MAPS_T','JEV_AB_DIR']:
   if k in os.environ:env[k]=os.environ[k]
  if execution_backend=='docker':command=docker_command(run,key,role)
  else:
   from sandbox import policy
   sp=Path(run)/'control/sandboxes'/(role+'.sb');sp.parent.mkdir(exist_ok=True);sp.write_text(policy(run,role))
   command=['/usr/bin/sandbox-exec','-f',str(sp),sys.executable,str(ROOT/'app/pipeline.py'),'worker',str(run),key]
  with log_path.open('a') as log:
   proc=subprocess.Popen(command,cwd=Path(run)/'workspaces'/role,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 except Exception as exc:
  from chrome import stop,role_port
  if browser_process:
   try:stop(browser_process,port=role_port(role))
   except Exception:pass # Teardown failure must never mask the original launch error.
  infra[tid]=infra.get(tid,0)+1;write(infra_path,infra)
  audit(run,role,'infra_start_failure',{'task':key,'category':'INFRA_START_FAILURE','infra_attempt':infra[tid],'error':str(exc)[:300]})
  raise
 # Worker launched successfully: only now does the semantic attempt count.
 attempts[tid]=attempts.get(tid,0)+1;write(attempts_path,attempts)
 return proc,browser_process

def resolve_run_path(run):
 return Path(run).resolve()

def _resume_backend(run,explicit_backend=None):
 import yaml
 from execution import backend,backend_was_explicit,runtime_details
 path=Path(run)/'run.yaml';manifest=yaml.safe_load(path.read_text())
 recorded=manifest.get('execution',{}).get('backend')
 if recorded:
  if backend_was_explicit(explicit_backend) and backend(explicit_backend)!=recorded:raise ValueError(f'Resume backend conflict: run requires {recorded}, requested {backend(explicit_backend)}')
  os.environ['LEADGEN_EXECUTION_BACKEND']=recorded
  return manifest['execution']
 if not backend_was_explicit(explicit_backend):raise ValueError('Legacy run has no recorded backend; resume requires explicit --backend local|docker')
 selected=backend(explicit_backend);details=runtime_details(selected);manifest['execution']=details
 path.write_text(yaml.safe_dump(manifest,sort_keys=False));os.environ['LEADGEN_EXECUTION_BACKEND']=selected
 return details

def _validate_recorded_runtime(details):
 from execution import runtime_details
 current=runtime_details(details.get('backend'),details.get('image'))
 if details.get('backend')=='docker' and current.get('image_digest')!=details.get('image_digest'):
  raise RuntimeError(f"Docker image digest mismatch: run requires {details.get('image_digest')}, current image is {current.get('image_digest')}")
 return current

def run_loop(run,selected_backend=None):
 # Must be absolute: worker processes are spawned with cwd=workspaces/<role>,
 # so a relative run path would make sandbox-exec resolve its -f profile
 # relative to that directory and every launch would die instantly.
 run=resolve_run_path(run);details=_resume_backend(run,selected_backend);_validate_recorded_runtime(details);spec=read(run/'control/spec.json');preflight(spec,browser_check=False,execution_details=details);conn=configure(run);graph=read(run/'control/graph.json')
 from execution import backend
 execution_backend=backend()
 record_metric(run,{'event':'execution_environment','at':now(),'execution':details})
 procs={};process_tasks={};proc_keys={};proc_roles={};browsers={};started={};timed_out=set()
 attempts_path=run/'control/attempts.json';attempts=read(attempts_path) if attempts_path.exists() else {}
 infra_path=run/'control/infra-attempts.json';infra=read(infra_path) if infra_path.exists() else {}
 # Kanban reclaims (and would re-spawn) any running task with no heartbeat
 # after stale_timeout_seconds; our workers never heartbeat, so the window
 # must exceed every per-task runtime cap or long discovery tasks would be
 # killed and duplicated mid-run.
 stale_seconds=stale_timeout()
 lanes=configured_concurrency() # bounded multi-lane dispatch (audit P1)
 peak_concurrent_tasks=0;peak_concurrent_browser_roles=0;queue_waits={};skipped=[]
 def business_slice_ids(node):
  try:
   bs=read(run/'raw/dedupe/dedupe.json')['businesses']
   return [b['business_id'] for b in bs[node['slice'][0]:node['slice'][1]]] if node.get('slice') else None
  except Exception:return None
 def queue_wait_seconds(node):
  # Time from the moment the task's last dependency completed (or creation
  # for roots) until the dispatcher claimed it. Pure telemetry; never gates.
  try:
   row=conn.execute('SELECT started_at,created_at FROM tasks WHERE id=?',(node['id'],)).fetchone()
   if not row or not row['started_at']:return None
   base=row['created_at'] or 0
   for pk in node.get('parents',[]):
    pr=conn.execute('SELECT completed_at FROM tasks WHERE id=?',(graph[pk]['id'],)).fetchone()
    if pr and pr['completed_at']:base=max(base,pr['completed_at'])
   return max(0,int(row['started_at']-base))
  except Exception:return None
 def spawn(task,workspace,board=None):
  # Peak gauges live in run_loop's scope; assignment here must update the
  # same variable, not shadow it (an unbound local read would abort every
  # spawn after the worker was already launched).
  nonlocal peak_concurrent_tasks,peak_concurrent_browser_roles
  key=next(k for k,n in graph.items() if n['id']==task.id);node=graph[key]
  proc,browser_process=launch_worker(run,key,node,attempts,infra,attempts_path,infra_path)
  procs[proc.pid]=proc;process_tasks[proc.pid]=task.id;proc_keys[proc.pid]=key;proc_roles[proc.pid]=node['role'];browsers[proc.pid]=browser_process;started[proc.pid]=time.monotonic()
  running=[pid for pid,p in procs.items() if p.poll() is None]
  peak_concurrent_tasks=max(peak_concurrent_tasks,len(running))
  peak_concurrent_browser_roles=max(peak_concurrent_browser_roles,len({proc_roles[pid] for pid in running if browsers.get(pid) is not None}))
  qw=queue_wait_seconds(node);queue_waits[proc.pid]=qw
  record_metric(run,{'event':'spawn','task':key,'task_id':task.id,'role':node['role'],'batch':node.get('batch'),'slice':node.get('slice'),'business_count':(node['slice'][1]-node['slice'][0]) if node.get('slice') else None,'business_ids':business_slice_ids(node),'task_class':node.get('task_class',role_class(node['role'])),'timeout_policy_seconds':node.get('timeout_seconds',TASK_CLASSES[role_class(node['role'])]),'exec_attempt':attempts[node['id']],'infra_attempt':infra.get(node['id'],0),'started_at':now(),'task_started_at':now(),'queue_wait_seconds':qw,'execution':details})
  return proc.pid
 lock=(run/'control/runner.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 try:
  while True:
   for pid,p in list(procs.items()):
    key=proc_keys[pid];node=graph[key];limit=node.get('timeout_seconds',TASK_CLASSES[role_class(node['role'])])
    if p.poll() is None and time.monotonic()-started[pid]>=limit:
     # Watchdog terminates only this worker's own process group.
     audit(run,'supervisor','worker_timeout',{'task_id':process_tasks[pid],'task':key,'category':'TASK_TIMEOUT','limit_seconds':limit});record_failure(run,task=key,failure_class='semantic',exit_code=None,stage='timeout',backend=execution_backend,log_path=run/'reports'/(key+'.log'),reason=f'worker exceeded {limit}s',failure_type='timeout');timed_out.add(pid);stop_worker(p)
    if p.poll() is not None:
     code=p.wait();tid=process_tasks.pop(pid);del procs[pid]
     from chrome import stop,role_port
     bp=browsers.pop(pid,None)
     # Teardown must never kill the run: a Chrome instance that already died
     # (pid recycled or reaped by the OS) can make killpg raise EPERM, which
     # previously aborted run_loop mid-flight and orphaned every other task.
     try:stop(bp,port=role_port(proc_roles.pop(pid)) if bp else None)
     except Exception as exc:audit(run,'supervisor','browser_teardown_error',{'task':key,'error':str(exc)[:200]})
     task_row=conn.execute('SELECT status,current_run_id FROM tasks WHERE id=?',(tid,)).fetchone();state=task_row['status']
     if execution_backend=='docker' and code==0 and state=='running':
      kb.complete_task(conn,tid,result=json.dumps({'output':node['output']}),expected_run_id=task_row['current_run_id'])
      state='done'
     incomplete_reason='iteration_exhausted' if record_iteration_exhaustion(run,key,node['role']) else None
     category='TASK_TIMEOUT' if pid in timed_out else None
     if code and not category:
      fp=run/'reports/failures'/(key+'.json')
      if fp.exists():
       failure=read(fp);category=failure.get('category','TASK_EXECUTION_FAILURE')
       if failure.get('class')=='infrastructure':
        attempts[tid]=max(0,attempts.get(tid,0)-1);infra[tid]=infra.get(tid,0)+1;write(attempts_path,attempts);write(infra_path,infra)
      else:
       category='TASK_EXECUTION_FAILURE';record_failure(run,task=key,failure_class='infrastructure',exit_code=code,stage='bootstrap',backend=execution_backend,log_path=run/'reports'/(key+'.log'),reason=f'worker exited {code} before recording a model/tool call')
     rt=round(time.monotonic()-started.pop(pid))
     record_metric(run,{'event':'finish','task':key,'task_id':tid,'role':node['role'],'task_class':node.get('task_class',role_class(node['role'])),'exec_attempt':attempts.get(tid,0),'infra_attempt':infra.get(tid,0),'finished_at':now(),'task_finished_at':now(),'runtime_seconds':rt,'task_duration_seconds':rt,'queue_wait_seconds':queue_waits.pop(pid,None),'final_state':state,'failure_category':category,'incomplete_reason':incomplete_reason})
     timed_out.discard(pid)
     if code and state=='running':kb.block_task(conn,tid,reason=f'Worker exited {code}; inspect role log',kind='capability')
     if code and attempts.get(tid,0)<MAX_EXEC_ATTEMPTS:
      kb.unblock_task(conn,tid);audit(run,'supervisor','retry',{'task_id':tid,'task':key,'attempt':attempts.get(tid,0)+1})
   from source_health import load_source_health, merge_source_health
   merge_source_health(run) # fold role-staged observations before the gate reads them
   skipped+=skip_blocked_source_tasks(run,conn,graph)
   result=kb.dispatch_once(conn,spawn_fn=spawn,max_in_progress=lanes,max_in_progress_per_profile=1,max_spawn=lanes,failure_limit=2,stale_timeout_seconds=stale_seconds)
   states=[dict(r) for r in conn.execute('SELECT id,title,status,last_failure_error FROM tasks')]
   running=[pid for pid,p in procs.items() if p.poll() is None]
   running_profiles=sorted({proc_roles[pid] for pid in running if browsers.get(pid) is not None})
   peak_concurrent_tasks=max(peak_concurrent_tasks,len(running))
   peak_concurrent_browser_roles=max(peak_concurrent_browser_roles,len(running_profiles))
   merge_source_health(run) # refresh after any worker-side observations this iteration
   if skipped:write(run/'reports/skipped-source-blocked.json',skipped) # cumulative; read by gate()'s run report
   write(run/'reports/status.json',{'at':now(),'configured_global_concurrency':lanes,'configured_per_profile_concurrency':1,'effective_concurrency':lanes,'currently_running_count':len(running),'running_tasks':sorted(proc_keys[pid] for pid in running),'running_profiles':running_profiles,'peak_concurrent_tasks':peak_concurrent_tasks,'peak_concurrent_browser_roles':peak_concurrent_browser_roles,'source_health':load_source_health(run),'skipped_source_blocked':skipped,'tasks':states})
   if all(r['status']=='done' for r in states):
    write(run/'reports/dispatch-summary.json',{'at':now(),'execution':details,'configured_global_concurrency':lanes,'configured_per_profile_concurrency':1,'effective_concurrency':lanes,'peak_concurrent_tasks':peak_concurrent_tasks,'peak_concurrent_browser_roles':peak_concurrent_browser_roles,'tasks_total':len(states),'tasks_done':sum(1 for r in states if r['status']=='done')})
    print('Complete:',run,flush=True);return
   if any(r['status'] in {'blocked','triage'} for r in states) and not procs:
    blocked=[r for r in states if r['status'] in {'blocked','triage'}];ensure_blocked_failure_records(run,blocked,graph,execution_backend);audit(run,'supervisor','blocked',{'tasks':blocked});print('Blocked; inspect',run/'reports/status.json',flush=True);raise SystemExit(2)
   if result.spawned:print('Started',[(x[0],x[1]) for x in result.spawned],flush=True)
   time.sleep(2)
 finally:
  from chrome import stop
  for p in procs.values():stop_worker(p)
  for pid,browser_process in list(browsers.items()):
   try:stop(browser_process,port=role_port(proc_roles.get(pid)) if proc_roles.get(pid) in READERS else None)
   except Exception:pass # Teardown is best-effort on shutdown; never mask the original error.
  conn.close();lock.close()

def main():
 parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='cmd',required=True)
 for cmd in ['preflight','start']:
  p=sub.add_parser(cmd);p.add_argument('spec');p.add_argument('--backend',choices=['docker','local'])
  if cmd=='start':p.add_argument('--repeat-businesses-from')
 for cmd in ['run','status']:
  p=sub.add_parser(cmd);p.add_argument('run')
  if cmd=='run':p.add_argument('--backend',choices=['docker','local'])
 p=sub.add_parser('worker');p.add_argument('run');p.add_argument('key')
 args=parser.parse_args()
 if args.cmd in {'preflight','start'}:
  import yaml
  spec=yaml.safe_load(Path(args.spec).read_text())
  if args.backend:os.environ['LEADGEN_EXECUTION_BACKEND']=args.backend
  if args.cmd=='preflight':print(json.dumps(preflight(spec),indent=2))
  else:
   run=make_run(spec,args.repeat_businesses_from,args.backend);print(run,flush=True);run_loop(run,args.backend)
 elif args.cmd=='worker':worker_main(args.run,args.key)
 elif args.cmd=='run':run_loop(args.run,args.backend)
 elif args.cmd=='status':
  conn=configure(args.run);print(json.dumps([dict(x) for x in conn.execute('SELECT id,title,status,result FROM tasks')],indent=2))
if __name__=='__main__':main()
