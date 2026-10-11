"""Orchestration scaling tests: batching, attempt budgets, timeouts, chrome lifecycle, bounded re-discovery. All mocked; no live web."""
import sys,tempfile,json,unittest,os
from pathlib import Path
from unittest.mock import patch,Mock,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import pipeline
from core import read,write,now
import hermes_home_fixture

SPEC=lambda n:{'count':n,'sources':['google_maps','web','instagram'],'country':'ID','category':'spa wellness massage','area':'Canggu, Bali','requested_fields':['business_name','instagram','whatsapp','public_contact','google_rating','google_review_count','dm_proxy','dm_measured','owner'],'dm_probe':False,'formats':['md','xlsx','pdf']}
BATCHED=['read-google','read-site','find-ig','read-ig','read-reviews','find-owner','resolve-ig','resolve-wa','resolve-owner','resolve-dm']

def build_graph(count,spec=None):
 tmp=Path(tempfile.mkdtemp(prefix='orch-'));(tmp/'runs').mkdir()
 local={'backend':'local','image':None,'image_digest':None,'limits':{'sandbox':'macos'}}
 with patch('pipeline.ROOT',tmp),patch.object(pipeline,'preflight',lambda s,browser_check=True,**kwargs:{'passed':True}),patch('execution.runtime_details',return_value=local):
  run=pipeline.make_run(spec or SPEC(count),selected_backend='local')
 return run,json.loads((run/'control/graph.json').read_text())

class BatchPlan(unittest.TestCase):
 def test_stage_specific_batch_sizes(self):
  self.assertEqual(pipeline.batch_size('find-ig'),5);self.assertEqual(pipeline.batch_size('read-ig'),5)
  self.assertEqual(pipeline.batch_size('read-reviews'),5);self.assertEqual(pipeline.batch_size('find-owner'),5)
  self.assertEqual(pipeline.batch_size('resolve-owner'),10)
  for role in ['read-google','read-site','resolve-ig','resolve-wa','resolve-dm']:self.assertEqual(pipeline.batch_size(role),25)
 def test_20_business_batches(self):
  run,g=build_graph(20)
  self.assertEqual([g[k]['slice'] for k in sorted(g) if k.startswith('find-ig-batch')],[[0,5],[5,10],[10,15],[15,20]])
  self.assertEqual([g[k]['slice'] for k in sorted(g) if k.startswith('read-google-batch')],[[0,20]])
  self.assertEqual([g[k]['slice'] for k in sorted(g) if k.startswith('resolve-owner-batch')],[[0,10],[10,20]])
 def test_60_business_batches(self):
  run,g=build_graph(60)
  self.assertEqual(len([k for k in g if k.startswith('find-ig-batch')]),12)
  self.assertEqual(len([k for k in g if k.startswith('resolve-owner-batch')]),6)
  self.assertEqual(len([k for k in g if k.startswith('read-google-batch')]),3)
 def test_every_business_exactly_once_per_stage(self):
  run,g=build_graph(60)
  for role in BATCHED:
   slices=[g[k]['slice'] for k in sorted(g) if k.startswith(role+'-batch')]
   flat=[x for s in slices for x in range(s[0],s[1])]
   self.assertEqual(flat,list(range(60)),role) # no loss, no duplication, no overlap
 def test_no_cross_role_batch_confusion(self):
  run,g=build_graph(60)
  for k,n in g.items():
   if '-batch-' in k:self.assertTrue(n['slice'][1]-n['slice'][0]<=pipeline.batch_size(n['role']),k)
 def test_batch_task_class_is_llm_browser_for_readers(self):
  run,g=build_graph(20)
  for role in ['find-ig','read-ig','read-reviews','find-owner']:
   k=role+'-batch-01';self.assertEqual(g[k]['task_class'],'reader_browser_llm');self.assertEqual(g[k]['timeout_seconds'],1800)
  self.assertEqual(g['read-google-batch-01']['task_class'],'deterministic')

class FanIn(unittest.TestCase):
 def test_downstream_deps_fan_in_from_all_overlapping_batches(self):
  run,g=build_graph(60)
  self.assertEqual([k for k in g['resolve-ig-batch-01']['parents'] if k.startswith('read-ig')],[f'read-ig-batch-{i:02d}' for i in range(1,6)])
  self.assertEqual([k for k in g['resolve-ig-batch-02']['parents'] if k.startswith('read-ig')],[f'read-ig-batch-{i:02d}' for i in range(6,11)])
  self.assertEqual([k for k in g['read-ig-batch-07']['parents'] if k.startswith('find-ig')],['find-ig-batch-07'])
 def test_validator_waits_for_all_batches(self):
  run,g=build_graph(60)
  vp=set(g['validator']['parents'])
  for k in g:
   if '-batch-' in k:self.assertIn(k,vp,k)
  self.assertIn('dedupe',vp)
 def test_consolidator_cannot_run_early(self):
  run,g=build_graph(20);self.assertEqual(g['consolidator']['parents'],['validator'])
 def test_supervisor_cannot_run_early(self):
  run,g=build_graph(20);self.assertEqual(g['supervisor']['parents'],['consolidator'])
 def test_renderers_cannot_run_before_approval(self):
  run,g=build_graph(20)
  for fmt in ['md','xlsx','pdf']:self.assertEqual(g['render-'+fmt]['parents'],['supervisor'])

class TimeoutPolicy(unittest.TestCase):
 def test_timeout_differs_by_task_class(self):
  run,g=build_graph(20)
  self.assertEqual(g['read-ig-batch-01']['timeout_seconds'],1800)
  self.assertEqual(g['find-maps']['timeout_seconds'],2700)
  self.assertEqual(g['query-plan']['timeout_seconds'],1200)
  self.assertEqual(g['resolve-owner-batch-01']['timeout_seconds'],1200)
  self.assertEqual(g['read-google-batch-01']['timeout_seconds'],900)
  self.assertEqual(g['render-md']['timeout_seconds'],900)
 def test_every_role_has_a_class_and_timeout(self):
  for k,n in build_graph(20)[1].items():self.assertEqual(n['timeout_seconds'],pipeline.TASK_CLASSES[pipeline.role_class(n['role'])],k)
 def test_stale_window_exceeds_every_timeout(self):
  self.assertGreaterEqual(pipeline.stale_timeout(),max(pipeline.TASK_CLASSES.values())+pipeline.STALE_MARGIN)
  self.assertEqual(pipeline.stale_timeout(),3300)

def run_dir(tmp):
 run=tmp/'run';[ (run/d).mkdir(parents=True) for d in ['control/sandboxes','reports','audit','raw','workspaces'] ];return run

class AttemptBudgets(unittest.TestCase):
 def setUp(self):
  self.backend_env=patch.dict(os.environ,{'LEADGEN_EXECUTION_BACKEND':'local'});self.backend_env.start();self.addCleanup(self.backend_env.stop)
 def launch(self,node,chrome_side=None,popen=None):
  tmp=Path(tempfile.mkdtemp());run=run_dir(tmp)
  attempts={};infra={}
  ap,ip=tmp/'a.json',tmp/'i.json'
  args=(run,node['key'],node,attempts,infra,ap,ip)
  with patch('chrome.start',side_effect=chrome_side or [Mock()]),patch('sandbox.policy',return_value='profile'),patch('pipeline.subprocess.Popen',return_value=popen or Mock()),patch('pipeline.time.sleep'):
   result=pipeline.launch_worker(*args)
  return result,attempts,infra
 def test_infra_spawn_failure_does_not_consume_task_attempt(self):
  # Two transient port collisions then success: the semantic budget must be
  # untouched until the worker actually launches (audit P1-2).
  bp=Mock()
  (proc,browser),attempts,infra=self.launch({'key':'k1','id':'t1','role':'read-ig'},chrome_side=[RuntimeError('Dedicated browser port 19326 is already occupied'),RuntimeError('Dedicated browser port 19326 is still occupied'),bp])
  self.assertEqual(attempts,{'t1':1});self.assertEqual(infra,{});self.assertIs(browser,bp)
 def test_bounded_infra_retry_then_explicit_limit(self):
  node={'key':'k1','id':'t1','role':'read-ig'}
  tmp=Path(tempfile.mkdtemp());run=run_dir(tmp);attempts={};infra={};calls=[]
  def boom(*a,**k):calls.append(1);raise RuntimeError('Dedicated browser port 19326 is already occupied')
  with patch('chrome.start',side_effect=boom),patch('sandbox.policy',return_value='p'),patch('pipeline.time.sleep'):
   for _ in range(4):
    with self.assertRaisesRegex(RuntimeError,'port 19326'):pipeline.launch_worker(run,node['key'],node,attempts,infra,tmp/'a.json',tmp/'i.json')
   self.assertEqual(infra,{'t1':4})
   with self.assertRaisesRegex(RuntimeError,'Infrastructure start limit reached'):pipeline.launch_worker(run,node['key'],node,attempts,infra,tmp/'a.json',tmp/'i.json')
  self.assertEqual(len(calls),4*3) # bounded: 3 browser tries per dispatch, never infinite
  self.assertEqual(attempts,{}) # no semantic attempt consumed by infra failures
 def test_task_execution_failure_consumes_attempt_and_stops_at_two(self):
  node={'key':'k1','id':'t1','role':'read-ig'}
  tmp=Path(tempfile.mkdtemp());run=run_dir(tmp);attempts={};infra={}
  with patch('chrome.start',return_value=Mock()),patch('sandbox.policy',return_value='p'),patch('pipeline.subprocess.Popen',return_value=Mock()):
   pipeline.launch_worker(run,node['key'],node,attempts,infra,tmp/'a.json',tmp/'i.json')
   pipeline.launch_worker(run,node['key'],node,attempts,infra,tmp/'a.json',tmp/'i.json')
   self.assertEqual(attempts,{'t1':2})
   with self.assertRaisesRegex(RuntimeError,'Two-attempt limit'):pipeline.launch_worker(run,node['key'],node,attempts,infra,tmp/'a.json',tmp/'i.json')
 def test_popen_failure_is_infrastructure_and_closes_browser(self):
  node={'key':'k1','id':'t1','role':'read-ig'};browser=Mock()
  tmp=Path(tempfile.mkdtemp());run=run_dir(tmp);attempts={};infra={}
  with patch('chrome.start',return_value=browser),patch('sandbox.policy',return_value='p'),patch('pipeline.subprocess.Popen',side_effect=OSError('fork failed')),patch('chrome.stop') as stop,patch('pipeline.time.sleep'):
   with self.assertRaises(OSError):pipeline.launch_worker(run,node['key'],node,attempts,infra,tmp/'a.json',tmp/'i.json')
  self.assertEqual(infra,{'t1':1});self.assertEqual(attempts,{});stop.assert_called_once()
 def test_infra_failure_of_non_browser_role_is_bounded_too(self):
  node={'key':'k1','id':'t1','role':'validator'}
  tmp=Path(tempfile.mkdtemp());run=run_dir(tmp);attempts={};infra={}
  with patch('sandbox.policy',return_value='p'),patch('pipeline.subprocess.Popen',side_effect=OSError('spawn failed')):
   with self.assertRaises(OSError):pipeline.launch_worker(run,node['key'],node,attempts,infra,tmp/'a.json',tmp/'i.json')
   self.assertEqual(infra,{'t1':1})
   with self.assertRaisesRegex(RuntimeError,'Infrastructure start limit reached'):pipeline.launch_worker(run,node['key'],node,attempts,{'t1':4},tmp/'a.json',tmp/'i.json')
  self.assertEqual(attempts,{})
 def test_failure_categories(self):
  import jsonschema
  f=pipeline.classify_failure
  self.assertEqual(f(json.JSONDecodeError('x','y',0)),'LLM_OUTPUT_INVALID')
  self.assertEqual(f(jsonschema.ValidationError('bad model json')),'LLM_OUTPUT_INVALID')
  self.assertEqual(f(ValueError('Validation rejected: nope')),'VALIDATION_FAILURE')
  self.assertEqual(f(ValueError('Semantic extraction incomplete for Maps metrics')),'SEMANTIC_FAILURE')
  self.assertEqual(f(ValueError('Discovery under-delivered: requested 60, discovered 40')),'SEMANTIC_FAILURE')
  self.assertEqual(f(RuntimeError('Dedicated browser port 19324 is already occupied')),'BROWSER_START_FAILURE')
  self.assertEqual(f(RuntimeError('Infrastructure start limit reached')),'INFRA_START_FAILURE')
  self.assertEqual(f(PermissionError('Probe/out-of-scope role disabled')),'TASK_EXECUTION_FAILURE')
  self.assertEqual(f(ValueError('Snapshot not found')),'TASK_EXECUTION_FAILURE')

class ChromeLifecycle(unittest.TestCase):
 def setUp(self):
  # chrome.start rewrites its own role profile's config.yaml; point that at a
  # throwaway Hermes home instead of the developer's.
  self.hermes_home=hermes_home_fixture.install(self)
 def test_release_touches_only_the_dedicated_instance(self):
  import chrome,signal,re
  userdir=str(Path.home()/'leadgen/browser/read-site');out=Mock(returncode=0,stdout='4242\n  4243\n');gone=Mock(returncode=1,stdout='')
  with patch('chrome.subprocess.run',side_effect=[out,gone]) as pgrep,patch('chrome.os.killpg') as killpg,patch('chrome.port_free',return_value=True),patch('chrome.time.sleep'):
   chrome.release_own_instance(19326,userdir)
  self.assertEqual(pgrep.call_args[0][0][2],re.escape('--user-data-dir='+userdir)) # exact own-instance match
  self.assertEqual([c.args for c in killpg.call_args_list],[(4242,signal.SIGTERM),(4243,signal.SIGTERM)]) # own groups only, nothing else is ever signalled
 def test_start_recovers_after_releasing_own_instance(self):
  import chrome
  fake=MagicMock();fake.__enter__.return_value.read.return_value=json.dumps({'webSocketDebuggerUrl':'ws://127.0.0.1:1'})
  proc=Mock();proc.poll.return_value=None
  with patch('chrome.port_free',side_effect=[False,True]) as pf,patch('chrome.release_own_instance') as rel,patch('urllib.request.urlopen',return_value=fake),patch('chrome.subprocess.Popen',return_value=proc),patch('chrome.time.sleep'):
   tmp=Path(tempfile.mkdtemp());(tmp/'reports').mkdir();p=chrome.start(tmp,'read-site')
  self.assertIs(p,proc);rel.assert_called_once()
 def test_start_refuses_when_port_never_released(self):
  import chrome
  blocked=Mock();blocked.bind.side_effect=OSError('address in use');blocked.__enter__=Mock(return_value=blocked);blocked.__exit__=Mock(return_value=False)
  with patch('chrome.port_free',return_value=False),patch('chrome.release_own_instance'),patch('chrome.socket.socket',return_value=blocked),patch('chrome.subprocess.Popen') as popen,patch('chrome.time.sleep'):
   tmp=Path(tempfile.mkdtemp());(tmp/'reports').mkdir()
   with self.assertRaisesRegex(RuntimeError,'still occupied'):chrome.start(tmp,'read-site')
  popen.assert_not_called()
 def test_stop_waits_bounded_for_port_release(self):
  import chrome,signal
  p=Mock();p.pid=7777;p.poll.return_value=None;p.wait.return_value=0
  ticks=iter([0,0,0,0,100])
  with patch('chrome.port_free',side_effect=[True,True]),patch('chrome.time.monotonic',side_effect=lambda:next(ticks)),patch('chrome.time.sleep') as slept,patch('chrome.os.killpg') as killpg:
   chrome.stop(p,port=19326)
  killpg.assert_called_once_with(7777,signal.SIGTERM) # SIGTERM only to this worker's own Chrome group
  p.wait.assert_called_once_with(timeout=5)
  self.assertFalse(slept.called) # port already released: no wait was needed
 def test_stop_survives_killpg_eperm_on_dead_instance(self):
  # run-20260922-133500: a Chrome that already died (pid recycled/reaped)
  # made os.killpg raise PermissionError, which crashed run_loop mid-flight
  # and orphaned the rest of the run. Teardown must be non-fatal.
  import chrome,signal,os as real_os
  p=Mock();p.pid=7777;p.poll.return_value=None;p.wait.return_value=0
  with patch('chrome.port_free',return_value=True),patch('chrome.os.killpg',side_effect=PermissionError(1,'Operation not permitted')) as killpg:
   chrome.stop(p,port=19326) # must not raise
  killpg.assert_called_once_with(7777,signal.SIGTERM)
 def test_stop_skips_killpg_when_already_exited(self):
  import chrome,signal
  p=Mock();p.pid=7777;p.poll.return_value=0;p.wait.return_value=0
  with patch('chrome.port_free',return_value=True),patch('chrome.os.killpg') as killpg:
   chrome.stop(p,port=19326)
  killpg.assert_not_called()

class ResumeSafety(unittest.TestCase):
 # run-20260922-133500 resume with a relative run path: workers are spawned
 # with cwd=workspaces/<role>, so sandbox-exec resolved its relative -f
 # profile against that cwd and every launch died in 2s as a fake
 # TASK_EXECUTION_FAILURE. run_loop must resolve the run path first.
 def test_run_loop_resolves_relative_path(self):
  tmp=Path(tempfile.mkdtemp());run,g=build_graph(5)
  import os
  old=os.getcwd();os.chdir(tmp)
  try:
   resolved=pipeline.resolve_run_path(os.path.relpath(run,tmp))
   self.assertTrue(resolved.is_absolute(),resolved)
   self.assertEqual(resolved,run.resolve())
  finally:
   os.chdir(old)

class BoundedRediscovery(unittest.TestCase):
 def stage(self,sufficient,candidates=None,count=5):
  tmp=Path(tempfile.mkdtemp());run=run_dir(tmp)
  spec=SPEC(count)
  write(run/'control/spec.json',spec)
  cands=candidates or [{'business_id':'b_%d'%i,'name_raw':'Spa %d'%i,'url':'https://s%d.example/'%i,'evidence_ids':['e_%d'%i]} for i in range(3)]
  write(run/'raw/dedupe/dedupe-1.json',{'candidates':cands,'discovered':len(cands),'sufficient':sufficient})
  write(run/'raw/find-web/find-web-round1.json',{'businesses':cands,'evidence':[]})
  out=str(run/'raw/find-web/find-web-r2.json')
  g={'dedupe-1':{'id':'t1','role':'dedupe','parents':[],'batch':None,'slice':None,'output':str(run/'raw/dedupe/dedupe-1.json')},
     'find-web-r2':{'id':'t2','role':'find-web','parents':['dedupe-1'],'batch':None,'slice':None,'task_class':'discovery','timeout_seconds':2700,'output':out}}
  write(run/'control/graph.json',g)
  return run,g,spec
 def test_second_round_skipped_when_round1_sufficient(self):
  run,g,spec=self.stage(True)
  with patch('worker.reader',side_effect=AssertionError('No browser work must run')) as rdr:
   out=pipeline.execute(run,'find-web-r2')
  self.assertEqual(out,{'businesses':[],'evidence':[]});rdr.assert_not_called()
 def test_second_round_runs_only_on_shortfall(self):
  run,g,spec=self.stage(False)
  seen={}
  def fake_reader(run,role,inputs,spec):
   seen.update(inputs);return {'businesses':[{'business_id':'b_9','name_raw':'Spa 9','url':'https://s9.example/','evidence_ids':['e9']}],'evidence':[]}
  with patch('worker.reader',side_effect=fake_reader):
   out=pipeline.execute(run,'find-web-r2')
  self.assertEqual(out['businesses'][0]['business_id'],'b_9')
  self.assertEqual(seen['rediscovery'],{'discovered':3,'requested':5,'names':['Spa 0','Spa 1','Spa 2']})
 def test_final_dedupe_merges_rounds_without_duplicates(self):
  run,g,spec=self.stage(False,count=3)
  write(run/'raw/find-web/find-web-r2.json',{'businesses':[{'business_id':'b_0','name_raw':'spa 0','url':'https://s0.example/','evidence_ids':['e0b']}],'evidence':[]})
  g['dedupe']={'id':'t3','role':'dedupe','parents':['dedupe-1','find-web-r2'],'batch':None,'slice':None,'task_class':'deterministic','timeout_seconds':900,'output':str(run/'raw/dedupe/dedupe.json')}
  write(run/'control/graph.json',g)
  out=pipeline.execute(run,'dedupe')
  self.assertEqual(len(out['businesses']),3) # b_0 rediscovered in round 2 merges into the same identity
  merged=[b for b in out['businesses'] if b['business_id']=='b_0'][0]
  self.assertEqual(sorted(merged['evidence_ids']),['e0b','e_0'])
 def test_final_dedupe_fails_early_on_shortfall(self):
  run,g,spec=self.stage(False,candidates=[{'business_id':'b_%d'%i,'name_raw':'Spa %d'%i,'url':'https://s%d.example/'%i,'evidence_ids':['e_%d'%i]} for i in range(3)])
  g['dedupe']={'id':'t3','role':'dedupe','parents':['dedupe-1'],'batch':None,'slice':None,'task_class':'deterministic','timeout_seconds':900,'output':str(run/'raw/dedupe/dedupe.json')}
  write(run/'control/graph.json',g)
  with self.assertRaisesRegex(ValueError,'Discovery under-delivered: requested 5, discovered 3'):
   pipeline.execute(run,'dedupe')
 def test_repeat_targets_still_enforced_after_two_rounds(self):
  cands=[{'business_id':'b_1','name_raw':'Spa 1','url':'https://s1.example/','evidence_ids':['e1']},{'business_id':'b_2','name_raw':'Spa 2','url':'https://s2.example/','evidence_ids':['e2']}]
  run,g,spec=self.stage(False,candidates=cands)
  write(run/'control/targets.json',[{'business_id':'b_1','name_raw':'Spa 1','url':'https://s1.example/'},{'business_id':'b_3','name_raw':'Spa 3','url':'https://s3.example/'}])
  g['dedupe']={'id':'t3','role':'dedupe','parents':['dedupe-1'],'batch':None,'slice':None,'task_class':'deterministic','timeout_seconds':900,'output':str(run/'raw/dedupe/dedupe.json')}
  write(run/'control/graph.json',g)
  with self.assertRaisesRegex(ValueError,'repeat-test identity'):pipeline.execute(run,'dedupe')

class Observability(unittest.TestCase):
 def test_metrics_record_contains_scaling_fields(self):
  tmp=Path(tempfile.mkdtemp());run=run_dir(tmp)
  pipeline.record_metric(run,{'event':'spawn','task':'read-ig-batch-01','task_id':'t1','role':'read-ig','batch':0,'slice':[0,5],'business_count':5,'business_ids':['b1','b2','b3','b4','b5'],'task_class':'reader_browser_llm','timeout_policy_seconds':1800,'exec_attempt':1,'infra_attempt':0,'started_at':now()})
  line=json.loads((run/'reports/run-metrics.jsonl').read_text().splitlines()[0])
  for k in ['role','batch','slice','business_count','business_ids','task_class','exec_attempt','infra_attempt','started_at','timeout_policy_seconds']:self.assertIn(k,line)
 def test_worker_failure_persisted_with_category(self):
  tmp=Path(tempfile.mkdtemp());run=run_dir(tmp)
  write(run/'control/graph.json',{'k1':{'id':'t1','role':'read-ig','parents':[],'batch':0,'slice':[0,5],'task_class':'reader_browser_llm','timeout_seconds':1800,'output':str(run/'raw/read-ig/k1.json')}})
  write(run/'control/spec.json',SPEC(5))
  # Kanban block path needs a db; simulate only the classification + file write used by worker_main.
  e=ValueError('Discovery under-delivered: requested 5, discovered 2')
  category=pipeline.classify_failure(e)
  write(run/'reports/failures'/'k1.json',{'task':'k1','role':'read-ig','category':category,'error':str(e),'at':now()})
  self.assertEqual(read(run/'reports/failures'/'k1.json')['category'],'SEMANTIC_FAILURE')

class Idempotency(unittest.TestCase):
 def test_retry_never_rewrites_a_completed_output(self):
  tmp=Path(tempfile.mkdtemp());p=tmp/'out.json'
  write(p,{'a':1},immutable=True)
  with self.assertRaisesRegex(ValueError,'Immutable output already exists'):write(p,{'a':2},immutable=True)

class SandboxFailureReports(unittest.TestCase):
 # run-20260922-115113: a sandboxed worker lost its failure report because
 # reports/failures did not exist (workers cannot mkdir) and was not a
 # permitted write path. make_run must pre-create it and the policy must
 # allow writes there, or failure categories degrade to TASK_EXECUTION_FAILURE.
 def test_failures_dir_precreated_and_writable(self):
  run,graph=build_graph(5)
  self.assertTrue((run/'reports/failures').is_dir())
  import sandbox
  for role in ('find-ig','read-site','resolve-ig','validator'):
   pol=sandbox.policy(str(run),role)
   self.assertIn(json.dumps(str((run/'reports'/'failures').resolve())),pol)

class ReadGoogleGate(unittest.TestCase):
 # run-20260922-125022: "Flare Wellness Canggu" matched two Maps listings
 # (Flare Wellness Canggu / Flare Wellness Sunset), so the strict identity
 # check refused it and the all-or-nothing gate blocked the entire 20-business
 # run twice, discarding 19 fully-extracted businesses. Identity-unverifiable
 # rows must become honest unavailability; extraction failures stay fatal.
 def test_identity_unverifiable_row_passes_and_is_marked(self):
  from quality import maps_semantic_success
  cov=[{'business_id':'b1','extracted':['google_rating','google_review_count','public_contact','website'],'missing':[],'reason':'','read_ok':True},
       {'business_id':'b2','extracted':[],'missing':['google_rating','google_review_count'],'reason':'Exact Maps business listing not uniquely identified'}]
  ok,rows=maps_semantic_success(cov)
  self.assertTrue(ok)
  self.assertFalse(rows[0].get('identity_unverifiable'))
  self.assertTrue(rows[1]['identity_unverifiable'])
 def test_extraction_failure_still_fails_gate(self):
  # Updated for the run-20260922-232944 semantic repair: read_ok marks a
  # successfully inspected verified listing, so honest metric absence
  # ("Not visible in verified listing overview") passes while actual read
  # failures and unknown/inconsistent states (no read_ok) stay fatal.
  from quality import maps_semantic_success
  base={'business_id':'b1','extracted':['google_rating','google_review_count'],'missing':[],'reason':'','read_ok':True}
  for reason in ('Maps listing heading missing',
                 "UnexpectedError: browser died mid-navigation"):
   ok,rows=maps_semantic_success([dict(base),{'business_id':'b2','extracted':[],'missing':['google_rating','google_review_count'],'reason':reason}])
   self.assertFalse(ok,reason)
   self.assertNotIn('identity_unverifiable',rows[1])
 def test_verified_row_with_missing_metrics_passes(self):
  from quality import maps_semantic_success
  ok,rows=maps_semantic_success([{'business_id':'b1','extracted':['public_contact','website'],'missing':['google_rating','google_review_count'],'reason':'Not visible in verified listing overview','read_ok':True}])
  self.assertTrue(ok);self.assertFalse(rows[0].get('identity_unverifiable'))
 def test_identity_failure_still_requires_full_identity_reasons(self):
  from quality import maps_semantic_success
  cov=[{'business_id':'b1','extracted':[],'missing':['google_rating'],'reason':'Maps result is not a business listing'}]
  ok,rows=maps_semantic_success(cov)
  self.assertTrue(ok);self.assertTrue(rows[0]['identity_unverifiable'])
  cov=[{'business_id':'b1','extracted':['google_review_count'],'missing':['google_rating'],'reason':'Maps listing location could not be corroborated'}]
  ok,rows=maps_semantic_success(cov)
  self.assertTrue(ok)

class LlmOutputReliability(unittest.TestCase):
 # Real failure in run-20260922-112747: the model escaped the newlines it saw
 # ('...ago\\n\\nHello...') and truncated with an ellipsis, so every owner_reply
 # observation was rejected and dm_proxy fell to not_public. Re-anchoring must
 # accept the de-escaped form while keeping the no-fabrication guarantee.
 def test_escaped_newlines_and_ellipsis_reanchor(self):
  from worker import reconcile_snippet
  obs='- text "Response from the owner a month ago"\n- text "Hello Salwa, thank you for choosing Face Therapy Spa"\n- text "Reply from the business owner"'
  snap={'observation':obs}
  o={'snippet':'- text "Response from the owner a month ago"\\n\\n- text "Hello Salwa, thank you for choosing Face Therapy Spa"\\n\\n… '}
  out=reconcile_snippet(o,snap)
  self.assertIn(out['snippet'],obs) # still a literal substring of the snapshot
  self.assertIn('Hello Salwa',out['snippet'])
 def test_genuine_fabrication_still_rejected(self):
  from worker import reconcile_snippet
  obs='- text "Response from the owner a month ago"\n- text "Hello Salwa"'
  # Fragment shares nothing with the snapshot: every tier must reject it.
  o={'snippet':'- text "The proprietor is Budi Santoso\\n\\nContact email: budi@example.com"'}
  with self.assertRaisesRegex(ValueError,'refusing to fabricate'):reconcile_snippet(o,{'observation':obs})
  o={'snippet':'- text "Owner: Made Sudira, founded 2019\\n\\nOwner email: budi@example.com"'}
  with self.assertRaisesRegex(ValueError,'refusing to fabricate'):reconcile_snippet(o,{'observation':obs})
 def test_literal_snippet_unchanged(self):
  from worker import reconcile_snippet
  obs='- text "Phone: +62 812 3899 7421"'
  o={'snippet':'- text "Phone: +62 812 3899 7421"'}
  self.assertEqual(reconcile_snippet(o,{'observation':obs})['snippet'],o['snippet'])
 def test_candidate_evidence_must_substantiate_the_discovered_name(self):
  # Real failure in run-20261011-023602: a discovery candidate cited a fragment
  # whose numeric-role rescue reconciled down to the rating badge
  # ('- image "4,6 bintang"'), dropping the name. The resolved business_name
  # takes name_raw as its value, so the validator then rejected it ("Value
  # absent from evidence") and the whole run died before the report. A
  # candidate's evidence must re-anchor to a genuine line carrying the name.
  from worker import reconcile_snippet, reconcile_candidate_snippet
  obs='- link "River Luxury Spa" [ref=e17]\n- image "4,6 bintang"\n- StaticText "River Luxury Spa"'
  o={'name_raw':'River Luxury Spa','snippet':'- image "4,6 bintang" [ref=e99]'}
  self.assertNotIn('River Luxury Spa',reconcile_snippet(dict(o),{'observation':obs})['snippet'])
  out=reconcile_candidate_snippet(dict(o),{'observation':obs})
  self.assertIn('River Luxury Spa',out['snippet'])
  self.assertIn(out['snippet'],obs) # still a literal substring of the snapshot
 def test_candidate_without_a_name_bearing_line_is_dropped_not_fabricated(self):
  from worker import reconcile_candidate_snippet
  obs='- image "4,6 bintang"\n- StaticText "Spa pijat  Continental Tower"'
  o={'name_raw':'River Luxury Spa','snippet':'- image "4,6 bintang"'}
  with self.assertRaisesRegex(ValueError,'name absent'):reconcile_candidate_snippet(o,{'observation':obs})

if __name__=='__main__':unittest.main()
