import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import sys,yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import execution,pipeline

DETAILS={'backend':'docker','image':'leadgen-worker:test','image_digest':'sha256:abc','limits':{'pids':256,'memory':'5g','cpus':2}}

class BackendPhase2(unittest.TestCase):
 def test_docker_details_requires_daemon_and_image(self):
  def result(code,out='',err=''):return type('R',(),{'returncode':code,'stdout':out,'stderr':err})()
  with patch.object(execution.shutil,'which',return_value='/usr/bin/docker'),patch.object(execution.subprocess,'run',side_effect=[result(0),result(0,'sha256:123\n')]):
   self.assertEqual(execution.docker_details()['image_digest'],'sha256:123')
  with patch.object(execution.shutil,'which',return_value='/usr/bin/docker'),patch.object(execution.subprocess,'run',return_value=result(1,err='offline')):
   with self.assertRaisesRegex(RuntimeError,'daemon is unavailable'):execution.docker_details()

 def run_dir(self,manifest):
  run=Path(tempfile.mkdtemp(prefix='backend-phase2-'));(run/'run.yaml').write_text(yaml.safe_dump(manifest));return run

 def test_resume_uses_recorded_backend_and_rejects_conflict(self):
  run=self.run_dir({'count':1,'execution':DETAILS})
  with patch.dict(os.environ,{},clear=True):self.assertEqual(pipeline._resume_backend(run)['backend'],'docker')
  with patch.dict(os.environ,{},clear=True):
   with self.assertRaisesRegex(ValueError,'Resume backend conflict'):pipeline._resume_backend(run,'local')

 def test_legacy_resume_requires_explicit_backend_then_persists(self):
  run=self.run_dir({'count':1})
  with patch.dict(os.environ,{},clear=True):
   with self.assertRaisesRegex(ValueError,'Legacy run has no recorded backend'):pipeline._resume_backend(run)
  with patch.dict(os.environ,{},clear=True),patch.object(execution,'runtime_details',return_value=DETAILS):self.assertEqual(pipeline._resume_backend(run,'docker'),DETAILS)
  self.assertEqual(yaml.safe_load((run/'run.yaml').read_text())['execution'],DETAILS)

 def test_recorded_image_digest_mismatch_is_refused(self):
  changed=dict(DETAILS,image_digest='sha256:changed')
  with patch.object(execution,'runtime_details',return_value=changed):
   with self.assertRaisesRegex(RuntimeError,'image digest mismatch'):pipeline._validate_recorded_runtime(DETAILS)

 def test_local_preflight_checks_update_health(self):
  with patch('hermes_cli.update_lock.update_in_progress',return_value=True):
   with self.assertRaisesRegex(RuntimeError,'update lock is active'):execution.local_details()

if __name__=='__main__':unittest.main()
