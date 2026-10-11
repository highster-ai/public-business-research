import sys,subprocess,tempfile,unittest,json,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from core import *
from sandbox import policy
class Runtime(unittest.TestCase):
 def test_cloud_only(self):
  verify_cloud_model(PROFILES['query-plan']['model'])
  for model in ['gpt-oss:20b','llama3:latest','glm-5.3-flash']:
   with self.assertRaises(PermissionError):verify_cloud_model({**PROFILES['query-plan']['model'],'default':model})
 @unittest.skipUnless(sys.platform=='darwin','the fail-closed sandbox boundary needs macOS sandbox-exec, which this platform does not have; the macOS CI job runs it and fails if it does not run')
 def test_os_boundaries(self):
  r=ROOT/'runs'/'test-sandbox'
  for d in ['control','raw/resolve-owner','workspaces/resolve-owner','canonical','audit']:(r/d).mkdir(parents=True,exist_ok=True)
  p=r/'control/test.sb';p.write_text(policy(r,'resolve-owner'))
  # The policy denies every read under the current user's home and re-allows
  # only what a role needs. Probe it with directories that always exist: a
  # credential file such as ~/.hermes/.env would raise "no such file" on a
  # machine that has none, and a sandboxed reader cannot tell that apart from a
  # denial -- the boundary would look intact without being exercised. The
  # release tree is probed alongside as a positive control, so a blanket
  # sandbox failure cannot masquerade as a denial of the home.
  code='''import os,socket,json
out={}
for name,path in [('home_read','HOME'),('repo_app_read','APP')]:
 try:
  os.listdir(path);out[name]='allowed'
 except PermissionError:out[name]='denied'
for name,path in [('canonical_write','RUN/canonical/denied'),('own_staging','RUN/raw/resolve-owner/allowed')]:
 try:
  open(path,'w').close();out[name]='allowed'
 except PermissionError:out[name]='denied'
try:socket.create_connection(('1.1.1.1',443),timeout=1);out['network']='allowed'
except PermissionError:out['network']='denied'
print(json.dumps(out))
'''.replace('RUN',str(r)).replace('APP',str(ROOT/'app')).replace('HOME',str(Path.home()))
  x=subprocess.run(['/usr/bin/sandbox-exec','-f',str(p),sys.executable,'-c',code],cwd=r/'workspaces/resolve-owner',capture_output=True,text=True,check=True)
  self.assertEqual(json.loads(x.stdout),{'home_read':'denied','repo_app_read':'allowed','canonical_write':'denied','own_staging':'allowed','network':'denied'})
 @unittest.skipUnless(sys.platform=='darwin','the fail-closed sandbox boundary needs macOS sandbox-exec, which this platform does not have; the macOS CI job runs it and fails if it does not run')
 def test_renderer_raw_denied(self):
  r=ROOT/'runs/test-sandbox';(r/'workspaces/render-md').mkdir(parents=True,exist_ok=True)
  # The renderer is denied another role's raw evidence. That evidence has to
  # exist, or the sandboxed read would raise "no such file" and the denial
  # would go unobserved -- as it did whenever this test ran before the one
  # that happens to leave the file behind.
  (r/'raw/resolve-owner').mkdir(parents=True,exist_ok=True)
  (r/'raw/resolve-owner/allowed').write_text('resolver-owned evidence\n')
  p=r/'control/render.sb';p.write_text(policy(r,'render-md'))
  code=f"try:\n open({str(r/'raw/resolve-owner/allowed')!r}).read()\n print('FAIL')\nexcept PermissionError:print('PASS')"
  x=subprocess.run(['/usr/bin/sandbox-exec','-f',str(p),sys.executable,'-c',code],cwd=r/'workspaces/render-md',capture_output=True,text=True,check=True)
  self.assertEqual(x.stdout.strip(),'PASS')
if __name__=='__main__':unittest.main()
