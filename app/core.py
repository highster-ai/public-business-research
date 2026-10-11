from pathlib import Path
import json, os, hashlib, datetime, re, urllib.parse, socket, ipaddress, csv
import jsonschema, phonenumbers
# Hermes plugin discovery is optional and unused by this project: the role
# charters grant no plugin tools and forbid web_search/web_extract, and the
# profiles are created with --no-skills and empty toolsets. Skipping it keeps
# host, worker and clean-clone environments deterministic, and keeps a bare
# clone quiet -- its plugins' own dependencies (httpx and friends) are not in
# requirements.lock and never will be, since no plugin is reachable here.
# setdefault, so an operator can still force discovery with HERMES_SAFE_MODE=0.
os.environ.setdefault('HERMES_SAFE_MODE','1')
ROOT=Path(os.environ.get('LEADGEN_ROOT',Path(__file__).resolve().parents[1])).resolve()
HERMES_HOME=Path(os.environ.get('HERMES_HOME',Path.home()/'.hermes')).resolve()
HERMES=Path(os.environ.get('LEADGEN_HERMES_ROOT',HERMES_HOME/'hermes-agent')).resolve()
PROFILES=json.loads((ROOT/'profiles.json').read_text())
READERS={r for r,p in PROFILES.items() if p['tools']}
FIELDS=['business_name','instagram','whatsapp','public_contact','google_rating','google_review_count','dm_proxy','dm_measured','owner']
PRIORITY={'official_site':0,'google_maps':1,'instagram':2,'public_source':3,'search_snippet':4}
OWNERS={'business_name':'consolidator','google_rating':'consolidator','google_review_count':'consolidator','instagram':'resolve-ig','whatsapp':'resolve-wa','public_contact':'resolve-wa','owner':'resolve-owner','dm_proxy':'resolve-dm','dm_measured':'probe-check'}
ALLOWED={'read-google':{'business_name','google_rating','google_review_count','public_contact','website'},'read-site':{'business_name','public_contact','whatsapp','instagram_candidate','owner','website'},'find-ig':{'instagram_candidate'},'read-ig':{'instagram','business_name','public_contact','whatsapp','posting_recency'},'read-reviews':{'owner_reply'},'find-owner':{'owner'},'find-maps':{'candidate'},'find-web':{'candidate'}}
def profile_charter(role):
 p=Path(PROFILES[role]['charter'])
 return p.resolve() if p.is_absolute() else (HERMES_HOME/p).resolve()
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def read(p):return json.loads(Path(p).read_text())
def write(p,v,immutable=False):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
 if immutable and p.exists():raise ValueError(f'Immutable output already exists: {p}')
 tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(v,indent=2,ensure_ascii=False));tmp.chmod(0o600);tmp.replace(p)
 if immutable:p.chmod(0o400)
def check(name,v):jsonschema.Draft202012Validator(read(ROOT/'schemas'/f'{name}.json'),format_checker=jsonschema.FormatChecker()).validate(v)
def contained(base,p):
 base=Path(base).resolve();p=Path(p).resolve()
 if not p.is_relative_to(base):raise ValueError('Path outside assigned role directory')
 return p
def audit(run,role,event,payload):
 # Per-role append-only chains avoid shared writer races.
 p=Path(run)/'audit'/f'{role}.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
 prev=json.loads(p.read_text().splitlines()[-1])['sha256'] if p.exists() and p.stat().st_size else ''
 item={'at':now(),'role':role,'event':event,'payload':payload,'previous':prev};item['sha256']=digest(item)
 with p.open('a') as f:f.write(json.dumps(item,ensure_ascii=False)+'\n')
 p.chmod(0o600)
def safe_url(url,role,resolve=True):
 u=urllib.parse.urlsplit(url);host=(u.hostname or '').lower()
 if u.scheme!='https' or u.username or u.password or u.port not in (None,443):raise ValueError('Only public HTTPS pages allowed')
 if not host or host in {'localhost'} or '.' not in host:raise ValueError('Private/local URL denied')
 denied=['gmail.com','mail.google.com','accounts.google.com','bank','api.','googleapis.com','serpapi','apify','brightdata','yelp.com','graph.facebook','web.whatsapp','wa.me','whatsapp.com']
 if any(x in host for x in denied) or re.search(r'/(api|graphql|oauth|login|signin|direct|messages|send)(/|\?|$)',u.path,re.I):raise ValueError('API, private account or messaging URL denied')
 if role in {'find-maps','read-google','read-reviews'} and not (host in {'www.google.com','google.com','maps.google.com'} and u.path.startswith('/maps')):raise ValueError('Google Maps-only browser scope; plain web search is not permitted for this role')
 if role=='read-ig' and host not in {'www.instagram.com','instagram.com'}:raise ValueError('Instagram-only scope')
 if role!='read-ig' and 'instagram.com' in host:raise ValueError('Instagram requires dedicated reader context')
 if resolve:
  for result in socket.getaddrinfo(host,443):
   if not ipaddress.ip_address(result[4][0]).is_global:raise ValueError('Non-public address denied')
 return url

def normalize_phone(value,country):
 if not isinstance(value,str):raise ValueError('Contact must be a string')
 value=value.strip()
 if country=='ID' and re.fullmatch(r'62[\d ()-]+',value):value='+'+value
 try:n=phonenumbers.parse(value,country)
 except phonenumbers.NumberParseException as exc:raise ValueError('Invalid public phone') from exc
 if not phonenumbers.is_valid_number(n):raise ValueError('Invalid public phone')
 return phonenumbers.format_number(n,phonenumbers.PhoneNumberFormat.E164)

def contact_values(e,field,country):
 # Only an explicit number/target in this evidence can establish WhatsApp.
 # A CTA in one record never converts a phone in another into WhatsApp.
 text=e['snippet'];values=set()
 if field=='whatsapp':
  chunks=re.findall(r'(?:https?://)?wa\.me/(\+?\d+)',text,re.I)
  chunks+=re.findall(r'(?:api\.|web\.)?whatsapp\.com/send/?\?[^\s"<>]*?phone=(\+?\d+)',urllib.parse.unquote(text),re.I)
  for line in text.splitlines():
   if re.search(r'\b(?:whatsapp|wa)\b',line,re.I):
    chunks+=re.findall(r'\+?\d[\d ()-]{6,}\d',line)
 else:chunks=re.findall(r'\+?\d[\d ()-]{6,}\d',text)
 for chunk in chunks:
  try:values.add(normalize_phone(chunk,country))
  except (ValueError,phonenumbers.NumberParseException):pass
 return values
def resolution(bid,field,status,value,refs,evidence,method,confidence=0):
 selected=[e for e in evidence if e['evidence_id'] in refs]
 return {'business_id':bid,'field':field,'status':status,'value':value,'confidence':confidence,'evidence_ids':refs,'resolver':OWNERS[field],'retrieved_at':max([e['retrieved_at'] for e in selected],default=now()),'resolved_at':now(),'method':method}
def validate_resolution(r,evidence,country):
 check('resolution',r)
 if r['resolver']!=OWNERS[r['field']]:raise ValueError('Unauthorized resolver')
 byid={e['evidence_id']:e for e in evidence}
 if any(i not in byid or byid[i]['business_id']!=r['business_id'] for i in r['evidence_ids']):raise ValueError('Missing/cross-business provenance')
 field=r['field']
 if field in {'whatsapp','public_contact'}:
  relevant={'whatsapp','public_contact'} if field=='whatsapp' else {'public_contact'}
  candidates=[e for e in evidence if e['business_id']==r['business_id'] and e['field'] in relevant and contact_values(e,field,country)]
  if candidates:
   best=min(PRIORITY[e['source_kind']] for e in candidates)
   top=[e for e in candidates if PRIORITY[e['source_kind']]==best]
   detail=f" ({r['business_id']}/{field}: usable higher-priority evidence is "+json.dumps([{'evidence_id':e['evidence_id'],'values':sorted(contact_values(e,field,country))} for e in top])+")"
   if r['status'] in {'not_found','not_public','access_restricted'} and not (r['status']=='access_restricted' and r['evidence_ids']):raise ValueError('Resolution skipped higher-priority source evidence'+detail)
   if not any(e['evidence_id'] in r['evidence_ids'] for e in top):raise ValueError('Resolution skipped higher-priority source evidence'+detail)
   if r['status']=='found' and not any(e['evidence_id'] in r['evidence_ids'] and r['value'] in contact_values(e,field,country) for e in top):raise ValueError('Resolution skipped higher-priority source evidence'+detail)
 if r['status']!='found':return
 v=r['value'];field=r['field'];selected=[byid[i] for i in r['evidence_ids']]
 if field=='google_rating' and (type(v) not in (int,float) or not 0<=v<=5):raise ValueError('Invalid rating type/range')
 if field=='google_review_count' and (type(v)!=int or v<0):raise ValueError('Invalid count type/range')
 if field in {'whatsapp','public_contact'}:
  if not isinstance(v,str) or normalize_phone(v,country)!=v:raise ValueError('Contact must be E.164')
 if field in {'business_name','instagram','owner'} and (not isinstance(v,str) or not v.strip()):raise ValueError('Expected nonempty string')
 if field=='instagram' and not re.fullmatch(r'https://www\.instagram\.com/[A-Za-z0-9_.]+/',v):raise ValueError('Invalid Instagram URL')
 if field=='dm_measured':raise ValueError('Probe disabled')
 if field=='dm_proxy':
  if not isinstance(v,dict) or set(v)!={'label','signals','score'} or v['label']!='Public activity proxy; not measured DM responsiveness' or v['score'] is not None:raise ValueError('Invalid proxy methodology')
  if set(v['signals'])!={'owner_reply','posting_recency'}:raise ValueError('Unexpected proxy component')
  for signal in v['signals'].values():
   if not isinstance(signal,dict) or set(signal)!={'status','evidence_ids'} or signal['status'] not in {'observed','unavailable'}:raise ValueError('Invalid proxy signal')
   if any(i not in r['evidence_ids'] for i in signal['evidence_ids']):raise ValueError('Proxy signal missing provenance')
 if field in {'google_rating','google_review_count'} and any(e['reader']!='read-google' or e['source_kind']!='google_maps' for e in selected):raise ValueError('Google metrics require Maps UI evidence')
 if field=='whatsapp' and not any(v in contact_values(e,field,country) for e in selected):raise ValueError('Phone is not proof of WhatsApp')
 if field=='owner' and not any(re.search(r'owner|founder|founded|pemilik|pendiri',e['snippet'],re.I) for e in selected):raise ValueError('No explicit owner/founder evidence')
 if field in {'whatsapp','public_contact'}:
  if not any(v in contact_values(e,field,country) for e in selected):raise ValueError('Phone value not supported by cited evidence')
 if field=='instagram':
  handle=v.rstrip('/').split('/')[-1]
  if not any(handle.casefold() in e['snippet'].casefold() for e in selected):raise ValueError('Instagram handle absent from evidence')
 relevant_fields={'instagram':{'instagram','instagram_candidate'},'owner':{'owner'},'whatsapp':{'whatsapp'},'public_contact':{'public_contact'}}.get(field,set())
 candidates=[e for e in evidence if e['business_id']==r['business_id'] and e['field'] in relevant_fields]
 if candidates and selected and min(PRIORITY[e['source_kind']] for e in selected)>min(PRIORITY[e['source_kind']] for e in candidates):raise ValueError('Resolution skipped higher-priority source evidence')
 if field=='instagram' and all(e['source_kind']=='search_snippet' for e in selected):raise ValueError('Search snippet alone cannot verify Instagram')
 if field not in {'dm_proxy','instagram','whatsapp','public_contact','google_rating','google_review_count'} and not any(v.casefold() in e['snippet'].casefold() for e in selected):raise ValueError('Value absent from evidence')

def verify_evidence(run,e):
 check('evidence',e)
 if e['field'] not in ALLOWED[e['reader']]:raise ValueError('Reader emitted unauthorized field')
 p=contained(Path(run)/'raw'/e['reader'],Path(run)/'raw'/e['reader']/ 'snapshots'/f"{e['snapshot_id']}.json")
 snap=read(p)
 if digest(snap['observation'])!=e['snapshot_sha256'] or e['snippet'] not in snap['observation'] or not e['snippet'].strip():raise ValueError('Evidence does not match immutable browser snapshot')
 if (e['source_url'],e['retrieved_at'],e['reader'])!=(snap['url'],snap['retrieved_at'],snap['reader']):raise ValueError('Forged retrieval metadata')
 host=urllib.parse.urlsplit(e['source_url']).hostname or ''
 if e['source_kind']=='google_maps' and not ('google.' in host and '/maps' in e['source_url']):raise ValueError('False Maps source classification')
 if e['source_kind']=='instagram' and host not in {'www.instagram.com','instagram.com'}:raise ValueError('False Instagram source classification')


def verify_cloud_model(model):
 policy=read(ROOT/'model-policy.json')
 if model.get('provider')!=policy['provider'] or model.get('base_url')!=policy['base_url'] or model.get('default') not in policy['allowed_models']:
  raise PermissionError('Cloud-only policy: model/provider/endpoint is not explicitly allowed; local fallback forbidden')
 return True
