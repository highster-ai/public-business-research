"""Entrypoint for one disposable Docker worker."""
import os,sys,subprocess,time,urllib.request
from pathlib import Path

ROOT=Path(os.environ['LEADGEN_ROOT'])
sys.path.insert(0,str(ROOT/'app'))

def start_browser():
 if os.environ.get('LEADGEN_ROLE') not in __import__('core').READERS:return None
 candidates=['/usr/bin/chromium','/usr/bin/chromium-browser','/usr/bin/google-chrome']
 candidates += [str(p) for p in Path('/opt/hermes/tools').glob('chromium-*/chrome-linux/chrome')]
 executable=next((p for p in candidates if Path(p).is_file()),None)
 if not executable:raise RuntimeError('Container image has no Chromium executable')
 port=9222;profile=Path('/tmp/chromium-profile');profile.mkdir(parents=True,exist_ok=True)
 proc=subprocess.Popen([executable,f'--remote-debugging-port={port}','--remote-debugging-address=127.0.0.1',
  f'--user-data-dir={profile}','--headless=new','--no-sandbox','--disable-dev-shm-usage','--no-first-run','about:blank'],
  stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT,start_new_session=True)
 for _ in range(100):
  if proc.poll() is not None:raise RuntimeError('Container Chromium exited during startup')
  try:
   urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version',timeout=.5).close()
   os.environ['BROWSER_CDP_URL']=f'http://127.0.0.1:{port}';return proc
  except Exception:time.sleep(.1)
 proc.terminate();raise RuntimeError('Container Chromium failed readiness check')

if __name__=='__main__':
 browser=start_browser()
 try:
  from pipeline import worker_main
  worker_main(sys.argv[1],sys.argv[2])
 finally:
  if browser and browser.poll() is None:browser.terminate()
