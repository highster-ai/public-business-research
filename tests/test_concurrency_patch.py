"""P0/P1/P2 concurrency-patch tests: bounded multi-lane dispatch, dispatch
telemetry, chrome lifecycle hardening, preflight concurrency envelope.
All mocked; no live web, no real Chrome launches."""
import sys,tempfile,json,os,sqlite3,re,unittest
from pathlib import Path
from unittest.mock import patch,Mock,MagicMock
ROOT_DIR=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT_DIR/'app'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import pipeline
from core import read,write,now,ROOT,READERS
import chrome
import hermes_home_fixture

SPEC=lambda n:{'count':n,'sources':['google_maps','web','instagram'],'country':'ID','category':'spa wellness massage','area':'Canggu, Bali','requested_fields':['business_name','instagram','whatsapp','public_contact','google_rating','google_review_count','dm_proxy','dm_measured','owner'],'dm_probe':False,'formats':['md','xlsx','pdf']}
# A recorded local runtime, as run.yaml carries it. `run_loop` revalidates the
# recorded backend against the host's live execution environment, which on a
# local run shells out to the installed Hermes CLI: stub it, so these dispatch
# wiring tests never depend on the machine having a Hermes installation.
LOCAL={'backend':'local','image':None,'image_digest':None,'limits':{'sandbox':'macos'}}

def build_graph(count,spec=None):
 tmp=Path(tempfile.mkdtemp(prefix='conc-'));(tmp/'runs').mkdir()
 with patch('pipeline.ROOT',tmp),patch.object(pipeline,'preflight',lambda s,browser_check=True,**kwargs:{'passed':True}),patch('execution.runtime_details',return_value=LOCAL):
  run=pipeline.make_run(spec or SPEC(count),selected_backend='local')
 return run,json.loads((run/'control/graph.json').read_text())

def run_dir(tmp):
 run=tmp/'run';[(run/d).mkdir(parents=True) for d in ['control/sandboxes','reports','audit','raw','workspaces']];return run


class ConcurrencyConfig(unittest.TestCase):
 def setUp(self):os.environ.pop(pipeline.CONCURRENCY_ENV,None)
 def tearDown(self):os.environ.pop(pipeline.CONCURRENCY_ENV,None)
 def test_default_is_three(self):
  self.assertEqual(pipeline.configured_concurrency(),3)
 def test_serial_rollback_mode_is_supported(self):
  os.environ[pipeline.CONCURRENCY_ENV]='1';self.assertEqual(pipeline.configured_concurrency(),1)
 def test_invalid_value_rejected(self):
  for bad in ['abc','3.5','one','2.0']:
   os.environ[pipeline.CONCURRENCY_ENV]=bad
   with self.assertRaises(ValueError):pipeline.configured_concurrency()
 def test_empty_value_means_default(self):
  os.environ[pipeline.CONCURRENCY_ENV]=''
  self.assertEqual(pipeline.configured_concurrency(),3)
 def test_cap_enforced(self):
  for bad in ['0','-2','5','99']:
   os.environ[pipeline.CONCURRENCY_ENV]=bad
   with self.assertRaises(ValueError):pipeline.configured_concurrency()
  os.environ[pipeline.CONCURRENCY_ENV]='4'
  self.assertEqual(pipeline.configured_concurrency(),4) # conservative cap itself is allowed
 def test_not_exposed_to_research_prompts(self):
  # The concurrency knob is dispatch infrastructure; it must never leak into
  # reader/resolve instructions.
  src=(ROOT_DIR/'app/worker.py').read_text()
  self.assertNotIn('LEADGEN_MAX_CONCURRENCY',src)
  self.assertNotIn('MAX_CONCURRENCY',src)


class DispatchWiring(unittest.TestCase):
 """run_loop must pass the configured lanes to the dispatcher with the
 same-role mutual exclusion kept at 1 — a hard invariant because same-role
 tasks share one dedicated Chrome port/user-data-dir."""
 def setUp(self):
  self.backend_env=patch.dict(os.environ,{'LEADGEN_EXECUTION_BACKEND':'local'});self.backend_env.start();self.addCleanup(self.backend_env.stop)
 def capture_run_loop(self,env=None):
  run,g=build_graph(5)
  captured={}
  def fake_dispatch(conn,**kw):
   captured.update(kw);raise SystemExit(0)
  old=os.environ.get(pipeline.CONCURRENCY_ENV)
  if env is None:os.environ.pop(pipeline.CONCURRENCY_ENV,None)
  else:os.environ[pipeline.CONCURRENCY_ENV]=env
  try:
   with patch.object(pipeline,'preflight',lambda s,browser_check=True,**kwargs:{'passed':True}),patch.object(pipeline.kb,'dispatch_once',side_effect=fake_dispatch),patch('execution.runtime_details',return_value=LOCAL):
    with self.assertRaises(SystemExit):pipeline.run_loop(str(run))
  finally:
   if old is None:os.environ.pop(pipeline.CONCURRENCY_ENV,None)
   else:os.environ[pipeline.CONCURRENCY_ENV]=old
  return captured
 def test_default_dispatch_params(self):
  kw=self.capture_run_loop()
  self.assertEqual(kw['max_in_progress'],3);self.assertEqual(kw['max_spawn'],3)
  self.assertEqual(kw['max_in_progress_per_profile'],1)
  self.assertEqual(kw['failure_limit'],2) # retry accounting unchanged
  self.assertEqual(kw['stale_timeout_seconds'],pipeline.stale_timeout())
 def test_serial_mode_dispatch_params(self):
  kw=self.capture_run_loop('1')
  self.assertEqual(kw['max_in_progress'],1);self.assertEqual(kw['max_spawn'],1)
  self.assertEqual(kw['max_in_progress_per_profile'],1)
 def test_lanes_capped(self):
  kw=self.capture_run_loop('4')
  self.assertEqual(kw['max_in_progress'],4);self.assertEqual(kw['max_spawn'],4)
 def test_real_spawn_records_metric_without_aborting(self):
  # Regression: peak gauges assigned inside the nested spawn() made the name
  # a local, so the first spawn raised UnboundLocalError AFTER the worker was
  # launched — dispatch recorded a spawn failure while the worker saw an
  # unclaimed task ("Task has not been claimed") and every task blocked.
  import types
  run,g=build_graph(5)
  proc=Mock();proc.pid=9204242;proc.poll.return_value=0;proc.wait.return_value=0
  def fake_dispatch(conn,spawn_fn=None,**kw):
   spawn_fn(types.SimpleNamespace(id=g['intake']['id']),str(run/'workspaces'/'intake'),board='leadgen')
   raise SystemExit(0)
  def fake_launch(run,key,node,attempts,infra,attempts_path,infra_path):
   attempts[node['id']]=attempts.get(node['id'],0)+1 # launch_worker counts the semantic attempt
   return proc,None
  with patch.object(pipeline,'preflight',lambda s,browser_check=True,**kwargs:{'passed':True}),patch.object(pipeline.kb,'dispatch_once',side_effect=fake_dispatch),patch.object(pipeline,'launch_worker',side_effect=fake_launch),patch('execution.runtime_details',return_value=LOCAL):
   with self.assertRaises(SystemExit):pipeline.run_loop(str(run))
  lines=[json.loads(l) for l in (run/'reports/run-metrics.jsonl').read_text().splitlines()]
  spawns=[l for l in lines if l.get('event')=='spawn']
  self.assertEqual(len(spawns),1)
  self.assertEqual(spawns[0]['task'],'intake')
  self.assertIn('queue_wait_seconds',spawns[0])
  self.assertEqual(spawns[0]['exec_attempt'],1)


class DispatchSemantics(unittest.TestCase):
 """Functional test against the real kanban dispatcher (Hermes code, unmodified):
 profiles are stubbed, spawn is recorded, no browser is launched."""
 def setUp(self):
  self.tmp=Path(tempfile.mkdtemp(prefix='disp-'))
  os.environ['HERMES_KANBAN_DB']=str(self.tmp/'kb.db')
  os.environ['HERMES_KANBAN_BOARD']='test-conc'
  self.conn=pipeline.kb.connect(self.tmp/'kb.db')
 def tearDown(self):self.conn.close()
 def task(self,title,assignee,parents=()):
  return pipeline.kb.create_task(self.conn,title=title,assignee=assignee,created_by='test',
   workspace_kind='dir',workspace_path=str(self.tmp),parents=parents,max_retries=2,
   max_runtime_seconds=600,idempotency_key=title)
 def dispatch(self):
  spawned=[]
  def rec(task,workspace,board=None):
   spawned.append(task.assignee);return os.getpid()
  with patch('hermes_cli.kanban_db_dispatch._memory_pressure_level',return_value='unknown'),patch('hermes_cli.profiles.profile_exists',lambda name:True):
   result=pipeline.kb.dispatch_once(self.conn,spawn_fn=rec,max_in_progress=3,
    max_spawn=3,max_in_progress_per_profile=1,failure_limit=2)
  return result,spawned
 def test_same_profile_never_concurrent(self):
  for t in ['a1','a2','b1','c1']:self.task(t,'p-'+t[0])
  result,spawned=self.dispatch()
  self.assertEqual(len(spawned),3) # global cap respected
  self.assertEqual(spawned.count('p-a'),1) # per-profile cap: a2 stayed queued
  self.assertEqual(len(set(spawned)),3) # three DISTINCT profiles ran concurrently
 def test_dependencies_still_respected(self):
  p=self.task('parent','p-a')
  self.task('child','p-b',parents=[p])
  result,spawned=self.dispatch()
  self.assertEqual(spawned,['p-a']) # child not spawnable while parent unfinished
  statuses={r['title']:r['status'] for r in [dict(x) for x in self.conn.execute('SELECT title,status FROM tasks')]}
  self.assertEqual(statuses['child'],'todo')
 def test_max_spawn_obeyed(self):
  for i in range(6):self.task(f'x{i}','p-%d'%i)
  result,spawned=self.dispatch()
  self.assertEqual(len(spawned),3)
  running=self.conn.execute("SELECT COUNT(*) c FROM tasks WHERE status='running'").fetchone()['c']
  self.assertEqual(running,3)


class ChromeIsolation(unittest.TestCase):
 def setUp(self):
  # chrome.start rewrites its own role profile's config.yaml, so it needs a
  # Hermes home. Use a throwaway one rather than the developer's.
  self.hermes_home=hermes_home_fixture.install(self)
 def test_role_ports_unique(self):
  ports=[chrome.role_port(r) for r in sorted(READERS)]
  self.assertEqual(len(ports),len(set(ports)))
  self.assertEqual(sorted(ports),list(range(19320,19320+len(READERS))))
 def test_user_data_dirs_role_isolated(self):
  for r in sorted(READERS):
   self.assertEqual(chrome.userdir_for_port(chrome.role_port(r)),ROOT/'browser'/r)
 def test_start_uses_only_its_own_role_userdir(self):
  # Every launch binds Chrome to ROOT/browser/<role> and its own port: two
  # concurrently dispatched roles can never share a profile directory.
  procs=[]
  def fake_popen(args,**kw):
   procs.append(args);p=Mock();p.pid=4242;p.poll.return_value=None;return p
  fake=MagicMock();fake.__enter__.return_value.read.return_value=json.dumps({'webSocketDebuggerUrl':'ws://x'})
  with patch('chrome.subprocess.Popen',side_effect=fake_popen),patch('urllib.request.urlopen',return_value=fake):
   for r in ['read-site','read-ig']:
    tmp=Path(tempfile.mkdtemp());(tmp/'reports').mkdir();chrome.start(tmp,r)
  udd=[a for args in procs for a in args if str(a).startswith('--user-data-dir=')]
  self.assertEqual(len(udd),2);self.assertEqual(len(set(udd)),2)
 def test_concurrent_outputs_disjoint(self):
  # Same-role tasks are mutually exclusive (DispatchSemantics), so only
  # different-role tasks can overlap in time; their writes must live in
  # disjoint files: task outputs, workspaces, sandbox profiles, logs.
  run,g=build_graph(20)
  roles={}
  for k,n in g.items():roles.setdefault(n['role'],[]).append(k)
  outs={k:n['output'] for k,n in g.items()}
  self.assertEqual(len(outs),len(set(outs.values())))
  for role,ks in roles.items():
   if len(ks)<2:continue
   # same role: shared sandbox profile is safe ONLY under same-role exclusivity
   self.assertTrue(all(g[k]['role']==role for k in ks))
  for a,b in [('read-site-batch-01','find-ig-batch-01'),('read-ig-batch-01','read-reviews-batch-01')]:
   self.assertNotEqual(g[a]['output'],g[b]['output'])
   self.assertNotEqual(g[a]['role'],g[b]['role'])


class PreflightEnvelope(unittest.TestCase):
 def setUp(self):
  self.backend_env=patch.dict(os.environ,{'LEADGEN_EXECUTION_BACKEND':'local'});self.backend_env.start();self.addCleanup(self.backend_env.stop)
 def test_envelope_counts_free_ports(self):
  with patch('chrome.port_free',return_value=True),patch('chrome.own_instance_pids',return_value=[]):
   k,av=pipeline.concurrency_envelope()
  self.assertEqual(k,3);self.assertEqual(len(av),len(READERS))
 def test_envelope_accepts_own_stale_instance(self):
  # A port held by OUR own dedicated instance is reclaimable: still viable.
  with patch('chrome.port_free',return_value=False),patch('chrome.own_instance_pids',return_value=[4242]):
   k,av=pipeline.concurrency_envelope()
  self.assertEqual(len(av),len(READERS))
 def test_envelope_rejects_foreign_port_holder(self):
  with patch('chrome.port_free',return_value=False),patch('chrome.own_instance_pids',return_value=[]):
   k,av=pipeline.concurrency_envelope()
  self.assertEqual(av,[])
 def test_envelope_fails_closed_below_k(self):
  # Fewer viable ports than lanes: the run must refuse to start.
  with patch('chrome.port_free',side_effect=lambda p:p in (19320,19321)),patch('chrome.own_instance_pids',return_value=[]):
   k,av=pipeline.concurrency_envelope()
  self.assertEqual(len(av),2)
  self.assertLess(len(av),k)


class ChromeTeardownHardening(unittest.TestCase):
 def test_stop_escalates_only_own_group(self):
  # Launcher ignored SIGTERM: SIGKILL must go to ITS process group only.
  import signal
  p=Mock();p.pid=7777;p.poll.return_value=None
  p.wait.side_effect=[__import__('subprocess').TimeoutExpired('x',5),0]
  with patch('chrome.os.killpg') as killpg,patch('chrome.port_free',return_value=True):
   chrome.stop(p,port=19326)
  calls=[(c.args[0],c.args[1]) for c in killpg.call_args_list]
  self.assertEqual(calls,[(7777,signal.SIGTERM),(7777,signal.SIGKILL)])
 def test_stop_waits_for_owned_pid_exit_before_escalating(self):
  import subprocess,signal
  p=Mock();p.pid=7777;p.poll.return_value=None
  p.wait.side_effect=[subprocess.TimeoutExpired('x',5),0]
  with patch('chrome.own_instance_pids',return_value=[]) as pgrep,patch('chrome.os.killpg') as killpg,patch('chrome.port_free',return_value=True):
   chrome.stop(p,port=19326)
  pgrep.assert_called() # explicit process-exit verification ran
  self.assertEqual(len(killpg.call_args_list),2)
 def test_stop_raises_when_owned_port_never_released(self):
  # Fail safely: a port still bound after bounded verification must be
  # reported, never silently handed to the next same-role task.
  p=Mock();p.pid=7777;p.poll.return_value=0 # already exited
  with patch('chrome.own_instance_pids',return_value=[]),patch('chrome.port_free',return_value=False),patch('chrome.time.sleep'):
   with self.assertRaisesRegex(RuntimeError,'not released'):chrome.stop(p,port=19326)
 def test_release_escalates_only_for_matching_userdir(self):
  import signal,re
  alive=Mock(returncode=0,stdout='4242\n');gone=Mock(returncode=1,stdout='')
  with patch('chrome.subprocess.run',side_effect=[alive,gone]) as pgrep,patch('chrome.os.killpg') as killpg,patch('chrome.port_free',return_value=True):
   chrome.release_own_instance(19326,str(ROOT/'browser/read-site'))
  self.assertEqual(pgrep.call_args[0][0][2],re.escape('--user-data-dir='+str(ROOT/'browser/read-site')))
  self.assertEqual([c.args for c in killpg.call_args_list],[(4242,signal.SIGTERM)])
 def test_release_kills_nothing_when_no_owned_instance(self):
  empty=Mock(returncode=1,stdout='')
  with patch('chrome.subprocess.run',return_value=empty) as pgrep,patch('chrome.os.killpg') as killpg:
   chrome.release_own_instance(19326,str(ROOT/'browser/read-site'))
  killpg.assert_not_called()
 def test_stop_ignores_unrelated_chrome(self):
  # pgrep matches ONLY our exact --user-data-dir; the user's normal Chrome
  # (no flag / different dir) can never be signalled. Contract: the pgrep
  # pattern is the exact userdir, and killpg only ever receives pids that
  # pattern returned.
  import signal
  p=Mock();p.pid=7777;p.poll.return_value=None;p.wait.return_value=0
  with patch('chrome.subprocess.run',return_value=Mock(returncode=0,stdout='')) as pgrep,patch('chrome.os.killpg') as killpg,patch('chrome.port_free',return_value=True):
   chrome.stop(p,port=19326)
  killpg.assert_called_once_with(7777,signal.SIGTERM) # only the launcher's own group
  self.assertIn(re.escape('--user-data-dir='+str(ROOT/'browser')+'/'),pgrep.call_args[0][0][2])
 def test_port_wait_is_bounded_not_infinite(self):
  self.assertLessEqual(chrome.PORT_WAIT_SECONDS,25)
  self.assertGreaterEqual(chrome.PORT_WAIT_SECONDS,15)
 def test_start_refuses_unknown_session_still(self):
  blocked=Mock();blocked.bind.side_effect=OSError('address in use');blocked.__enter__=Mock(return_value=blocked);blocked.__exit__=Mock(return_value=False)
  with patch('chrome.port_free',return_value=False),patch('chrome.release_own_instance'),patch('chrome.socket.socket',return_value=blocked),patch('chrome.subprocess.Popen') as popen,patch('chrome.time.sleep'):
   tmp=Path(tempfile.mkdtemp());(tmp/'reports').mkdir()
   with self.assertRaisesRegex(RuntimeError,'still occupied'):chrome.start(tmp,'read-site')
  popen.assert_not_called()


class TelemetryObservability(unittest.TestCase):
 def test_status_json_contract(self):
  tmp=Path(tempfile.mkdtemp());run=run_dir(tmp)
  # The run_loop status snapshot must carry the mandated concurrency fields.
  import inspect
  src=(ROOT_DIR/'app/pipeline.py').read_text()
  for key in ['configured_global_concurrency','configured_per_profile_concurrency','effective_concurrency','currently_running_count','running_tasks','running_profiles','peak_concurrent_tasks','peak_concurrent_browser_roles']:
   self.assertIn(key,src)
 def test_metrics_carry_task_lifecycle_fields(self):
  tmp=Path(tempfile.mkdtemp());run=run_dir(tmp)
  pipeline.record_metric(run,{'event':'spawn','task':'read-ig-batch-01','task_id':'t1','role':'read-ig','exec_attempt':1,'infra_attempt':0,'started_at':now(),'task_started_at':now(),'queue_wait_seconds':12})
  pipeline.record_metric(run,{'event':'finish','task':'read-ig-batch-01','task_id':'t1','role':'read-ig','exec_attempt':1,'infra_attempt':0,'finished_at':now(),'task_finished_at':now(),'runtime_seconds':95,'task_duration_seconds':95,'queue_wait_seconds':12,'final_state':'done','failure_category':None})
  lines=[json.loads(l) for l in (run/'reports/run-metrics.jsonl').read_text().splitlines()]
  for key in ['task_started_at','queue_wait_seconds']:self.assertIn(key,lines[0])
  for key in ['task_finished_at','task_duration_seconds','queue_wait_seconds']:self.assertIn(key,lines[1])
 def test_preflight_reports_concurrency_fields(self):
  src=(ROOT_DIR/'app/pipeline.py').read_text()
  for key in ["'configured_concurrency':k","'effective_concurrency':k","'configured_per_profile_concurrency':1","'available_role_ports':sorted(available)"]:
   self.assertIn(key,src)
 def test_dm_probe_untouched_and_no_probe_tasks(self):
  run,g=build_graph(5)
  self.assertFalse(any(n['role'].startswith('probe') for n in g.values()))
  self.assertFalse(SPEC(5)['dm_probe'])
 def test_validator_supervisor_chain_unchanged(self):
  run,g=build_graph(5)
  self.assertEqual(g['consolidator']['parents'],['validator'])
  self.assertEqual(g['supervisor']['parents'],['consolidator'])
  self.assertEqual(g['render-md']['parents'],['supervisor'])
 def test_jev_flags_not_in_dispatch_path(self):
  # The concurrency patch must not touch Jev: shadow collection stays gated
  # exactly as before (only when the operator exports the flags).
  src=(ROOT_DIR/'app/pipeline.py').read_text()
  self.assertIn("for k in ['JEV_SHADOW_ENABLED'",src)


if __name__=='__main__':unittest.main()
