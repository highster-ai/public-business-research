import sys,subprocess,tempfile,unittest,json,os
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from core import *
from sandbox import policy

# One process is spawned per probe, so these print exactly one word. 'missing'
# is kept distinct from 'denied': a boundary that has not been exercised looks
# identical to a boundary that held, if the only observed outcome is an error.
READ="import sys\ntry:\n open({path!r}).read();print('allowed')\nexcept PermissionError:print('denied')\nexcept FileNotFoundError:print('missing')"
WRITE="import sys\ntry:\n open({path!r},'w').close();print('allowed')\nexcept PermissionError:print('denied')\nexcept FileNotFoundError:print('missing')"
def probe(profile,run,role,code,env=None):
 x=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),sys.executable,'-c',code],cwd=Path(run)/'workspaces'/role,env=env,capture_output=True,text=True,check=True)
 return x.stdout.strip()
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
 @unittest.skipUnless(sys.platform=='darwin','the fail-closed sandbox boundary needs macOS sandbox-exec, which this platform does not have; the macOS CI job runs it and fails if it does not run')
 def test_home_below_tmp_is_unreadable(self):
  # The read denial is derived from Path.home(), so a home that itself lives
  # under /tmp is precisely the case the machine-wide temporary allowance used
  # to re-open. Generate the profile with HOME pointed at such a home: the
  # denial must name it, and nothing may re-allow it afterwards.
  r=ROOT/'runs/test-sandbox'
  for d in (r/'workspaces/resolve-owner',r/'control'):d.mkdir(parents=True,exist_ok=True)
  home=Path(tempfile.mkdtemp(prefix='tmp-home-',dir='/private/tmp'));(home/'.hermes').mkdir();(home/'.hermes'/'.env').write_text('HOME-SECRET\n')
  p=r/'control/home-under-tmp.sb'
  with patch.dict(os.environ,{'HOME':str(home)}):p.write_text(policy(r,'resolve-owner'))
  self.assertEqual(probe(p,r,'resolve-owner',READ.format(path=str(home/'.hermes'/'.env')),env={**os.environ,'HOME':str(home)}),'denied')
 @unittest.skipUnless(sys.platform=='darwin','the fail-closed sandbox boundary needs macOS sandbox-exec, which this platform does not have; the macOS CI job runs it and fails if it does not run')
 def test_temporary_directory_is_role_scoped(self):
  # A role gets its own temporary directory and no other role's. Both halves
  # matter: the first alone would pass if the policy simply denied /tmp, and
  # the second alone would pass if it granted all of it.
  r=ROOT/'runs/test-sandbox';own=r/'tmp/resolve-owner';other=r/'tmp/read-google'
  for d in (r/'workspaces/resolve-owner',r/'control',own,other):d.mkdir(parents=True,exist_ok=True)
  (other/'scratch').write_text('another role scratch\n')
  p=r/'control/role-tmp.sb';p.write_text(policy(r,'resolve-owner'))
  self.assertEqual(probe(p,r,'resolve-owner',WRITE.format(path=str(own/'mine'))),'allowed')
  self.assertEqual(probe(p,r,'resolve-owner',READ.format(path=str(own/'mine'))),'allowed')
  self.assertEqual(probe(p,r,'resolve-owner',READ.format(path=str(other/'scratch'))),'denied')
  self.assertEqual(probe(p,r,'resolve-owner',WRITE.format(path=str(other/'intruded'))),'denied')
 @unittest.skipUnless(sys.platform=='darwin','the fail-closed sandbox boundary needs macOS sandbox-exec, which this platform does not have; the macOS CI job runs it and fails if it does not run')
 def test_unrelated_private_tmp_file_is_denied(self):
  # /private/tmp holds every local process's scratch files. A directory made
  # there by something else stands in for them; the sandboxed role may not
  # read it, create inside it, or overwrite it.
  r=ROOT/'runs/test-sandbox'
  for d in (r/'workspaces/resolve-owner',r/'control'):d.mkdir(parents=True,exist_ok=True)
  foreign=Path(tempfile.mkdtemp(prefix='other-process-',dir='/private/tmp'));(foreign/'unrelated').write_text('another local process\n')
  p=r/'control/foreign-tmp.sb';p.write_text(policy(r,'resolve-owner'))
  self.assertEqual(probe(p,r,'resolve-owner',READ.format(path=str(foreign/'unrelated'))),'denied')
  self.assertEqual(probe(p,r,'resolve-owner',WRITE.format(path=str(foreign/'unrelated'))),'denied')
  self.assertEqual(probe(p,r,'resolve-owner',WRITE.format(path=str(foreign/'created'))),'denied')
 @unittest.skipUnless(sys.platform=='darwin','the fail-closed sandbox boundary needs macOS sandbox-exec, which this platform does not have; the macOS CI job runs it and fails if it does not run')
 def test_canonical_write_denied_for_non_consolidator(self):
  # Canonical output is the consolidator's alone. The denial has to survive the
  # temporary-directory change, which it does only because it is written after
  # the allow rules: sandbox rules are last-match-wins. The consolidator is
  # probed alongside as a positive control, so a blanket write denial cannot
  # pass this test by breaking the role that must write there.
  r=ROOT/'runs/test-sandbox';(r/'canonical').mkdir(parents=True,exist_ok=True);(r/'control').mkdir(parents=True,exist_ok=True)
  for role in ('resolve-owner','consolidator'):(r/'workspaces'/role).mkdir(parents=True,exist_ok=True)
  (r/'canonical/leads.json').write_text('{}\n')
  p=r/'control/canonical-deny.sb';p.write_text(policy(r,'resolve-owner'))
  self.assertEqual(probe(p,r,'resolve-owner',WRITE.format(path=str(r/'canonical/injected.json'))),'denied')
  q=r/'control/canonical-allow.sb';q.write_text(policy(r,'consolidator'))
  self.assertEqual(probe(q,r,'consolidator',WRITE.format(path=str(r/'canonical/owned.json'))),'allowed')
if __name__=='__main__':unittest.main()
