"""Fail-closed Python audit boundary around model execution.
Agents have no arbitrary code, terminal, filesystem, or delegation tools.
Trusted browser driver descendants remain part of the trusted computing base.
"""
import sys,os,socket
from pathlib import Path
from core import ROOT,HERMES
class Guard:
 def __init__(self,run,role,profile,browser=False):
  self.active=False;self.role=role;self.browser=browser
  self.run=Path(run).resolve();self.profile=Path(profile).resolve()
  self.writable=[Path('/dev/null'),self.run/'raw'/role,self.run/'audit'/f'{role}.jsonl',self.profile/'logs',self.profile/'cache',self.profile/'sessions',self.profile/'runtime',self.profile/'image_cache',self.profile/'browser',self.profile/'state.db',self.profile/'state.db-wal',self.profile/'state.db-shm']
  self.home=Path.home().resolve()
  self.allowed_network_hosts={'127.0.0.1','::1','localhost'}
  if os.environ.get('LEADGEN_CONTAINER')=='1':
   self.allowed_network_hosts.add('host.docker.internal')
   try:self.allowed_network_hosts.update(x[4][0] for x in socket.getaddrinfo('host.docker.internal',11434))
   except socket.gaierror:pass
  self.readable=[self.run/'audit'/f'{role}.jsonl',Path(sys.base_prefix).resolve(),ROOT/'app',ROOT/'schemas',ROOT/'venv',ROOT/'profiles.json',ROOT/'model-policy.json',HERMES,self.profile,self.run/'raw'/role,self.run/'workspaces'/role]
  sys.addaudithook(self.hook)
 def inside(self,path,roots):return any(path==r or path.is_relative_to(r) for r in roots)
 def hook(self,event,args):
  if not self.active:return
  if event=='socket.connect':
   address=args[1]
   if isinstance(address,tuple) and address[0] not in self.allowed_network_hosts:raise PermissionError('Worker network denied; only configured model/browser endpoints allowed')
  if event in {'os.system','os.exec','os.posix_spawn'}:raise PermissionError('Worker process execution denied')
  if event=='subprocess.Popen':
   argv=args[1]
   # Browser helper launches fixed commands; no shell and no arbitrary interpreter.
   allowed=('agent-browser','npx','node')
   executable=Path(str(argv[0])).name
   if not self.browser or not (any(x in executable for x in allowed) or executable in {'which'}):raise PermissionError('Worker subprocess outside browser driver denied')
   # The current Hermes browser driver uses an internal `eval` subcommand for
   # fixed navigation metadata.  It is safe here because the model only sees
   # our three narrow leadgen handlers; no evaluate/console tool is registered.
   if any(x in argv for x in ['request','exec']):raise PermissionError('Unsafe browser command denied')
  if event=='open':
   raw,mode,flags=args
   if not isinstance(raw,(str,bytes,os.PathLike)):return
   p=Path(os.fsdecode(raw)).resolve()
   writing=bool(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
   if writing and not self.inside(p,self.writable):
   # Fixed Hermes browser driver uses private temporary socket/session files.
    browser_tmp=(str(p).startswith('/private/var/folders/') or str(p).startswith('/private/tmp/'))
    if os.environ.get('LEADGEN_CONTAINER')=='1':browser_tmp=browser_tmp or p==Path('/tmp') or p.is_relative_to('/tmp')
    if not(self.browser and browser_tmp):raise PermissionError('Write outside role staging/runtime denied: '+str(p))
   if not writing and p.is_relative_to(self.home) and not self.inside(p,self.readable):
    if not(self.browser and (p.is_relative_to(self.home/'.npm') or p.is_relative_to(self.home/'.cache') or p.is_relative_to(self.home/'.hermes/node'))):raise PermissionError('Read outside role inputs/runtime denied: '+str(p))
 def __enter__(self):self.active=True;return self
 def __exit__(self,*args):self.active=False
