import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import execution,pipeline
import hermes_home_fixture

class DockerExecution(unittest.TestCase):
 def setUp(self):
  # Preflight audits every profile's config.yaml. Give it a throwaway
  # HERMES_HOME so this test never reads the developer's real one and runs on
  # a machine where no profile was ever provisioned.
  self.hermes_home=hermes_home_fixture.install(self)
 def test_docker_is_actually_available_where_required(self):
  # The isolation backend is the default for real runs, so the environment that
  # claims to exercise it must really provide a daemon: CI sets
  # LEADGEN_REQUIRE_DOCKER=1, which turns a missing daemon into a failure
  # instead of a skip that would let the Docker tests quietly stop running.
  if execution.docker_available():return
  if os.environ.get('LEADGEN_REQUIRE_DOCKER')=='1':self.fail('Docker is unavailable but this environment requires a working daemon')
  self.skipTest('Docker is not available in this environment')
 def test_docker_is_default(self):
  with patch.dict(os.environ,{},clear=True):self.assertEqual(execution.backend(),'docker')
 def test_backend_is_fail_closed(self):
  with patch.dict(os.environ,{execution.BACKEND_ENV:'invalid'}):
   with self.assertRaises(ValueError):execution.backend()
 def test_one_disposable_hardened_container_per_task(self):
  run=Path(tempfile.mkdtemp(prefix='docker-run-'))
  command=execution.docker_command(run,'read-site-01','read-site')
  self.assertEqual(command[:3],['docker','run','--rm'])
  self.assertIn('--cap-drop=ALL',command);self.assertIn('--security-opt=no-new-privileges',command)
  self.assertNotIn(str(Path.home()/'.hermes'),command)
  self.assertNotIn(f'src={execution.ROOT},dst={execution.ROOT}',command)
  self.assertIn('LEADGEN_HERMES_ROOT=/opt/hermes',command)
  self.assertEqual(command[-3:],['leadgen-worker:local',str(run.resolve()),'read-site-01'])
 def test_docker_preflight_fails_when_daemon_missing(self):
  spec={'count':1,'sources':['web'],'country':'ID','category':'spa','area':'Bali','requested_fields':['business_name'],'dm_probe':False,'formats':['md']}
  with patch.dict(os.environ,{execution.BACKEND_ENV:'docker'}),patch.object(execution,'docker_details',side_effect=RuntimeError('Docker daemon is unavailable')):
   with self.assertRaisesRegex(RuntimeError,'Docker daemon is unavailable'):
    pipeline.preflight(spec,browser_check=False)
