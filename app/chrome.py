from core import *
import subprocess,urllib.request,time,yaml,socket,signal
CHROME='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
# Bounded lifecycle budgets: a slow-dying previous dedicated Chrome must not
# wedge the dispatcher, and a busy port must never be attached to blindly.
# 25s (audit P2): the happy path is explicit process-exit + port-release
# verification; the longer window is only a bounded last resort, not a sleep
# we lean on. The pre-P2 15s window was measured as too short on macOS:
# 15 infra_start_retry events and 2 full batch re-runs in run-20260924-024804.
PORT_WAIT_SECONDS=25
GROUP_EXIT_WAIT_SECONDS=5
def port_free(port):
 # SO_REUSEADDR is required: after a dedicated Chrome exits, its closed
 # DevTools connections linger in server-side TIME_WAIT on this exact port
 # (the CDP probe sockets above), and without SO_REUSEADDR a plain bind
 # reports the port occupied for tens of seconds even though the listener is
 # gone — which is precisely what produced the forensic run's 15
 # infra_start_retry events and spurious "port not released" failures.
 # SO_REUSEADDR still refuses to bind while a LIVE listener holds the port.
 probe=socket.socket()
 try:probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);probe.bind(('127.0.0.1',port));return True
 except OSError:return False
 finally:probe.close()
def own_instance_pids(userdir):
 # Only processes whose command line carries THIS role's exact user-data-dir.
 # The user's normal Chrome and every other dedicated instance never match
 # and are never signalled by anything in this module.
 try:
  out=subprocess.run(['/usr/bin/pgrep','-f',re.escape(f'--user-data-dir={userdir}')],capture_output=True,text=True)
  return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
 except Exception:return []
def signal_own_groups(pids,sig):
 for pid in pids:
  try:os.killpg(pid,sig) # Launcher used start_new_session=True, so pgid==pid.
  except (ProcessLookupError,PermissionError):pass
def wait_own_exit(userdir,deadline):
 # Explicit process-exit verification for the owned groups (bounded).
 while time.monotonic()<deadline:
  if not own_instance_pids(userdir):return True
  time.sleep(.25)
 return not own_instance_pids(userdir)
def release_own_instance(port,userdir):
 # Terminate the dedicated Hermes Chrome launched with THIS role's exact
 # user-data-dir: SIGTERM the owned process group, VERIFY process exit, and
 # only then escalate to SIGKILL — always restricted to the same owned
 # groups, never a blanket kill and never any unrelated Chrome process.
 pids=own_instance_pids(userdir)
 signal_own_groups(pids,signal.SIGTERM)
 if not wait_own_exit(userdir,time.monotonic()+GROUP_EXIT_WAIT_SECONDS):
  signal_own_groups(own_instance_pids(userdir),signal.SIGKILL)
  wait_own_exit(userdir,time.monotonic()+GROUP_EXIT_WAIT_SECONDS)
 deadline=time.monotonic()+PORT_WAIT_SECONDS
 while time.monotonic()<deadline and not port_free(port):time.sleep(.5)
def role_port(role):return 19320+sorted(READERS).index(role)
def userdir_for_port(port):
 roles=sorted(READERS);idx=port-19320
 return ROOT/'browser'/roles[idx] if 0<=idx<len(roles) else None
def start(run,role):
 if role not in READERS:return None
 port=role_port(role);userdir=ROOT/'browser'/role;userdir.mkdir(parents=True,exist_ok=True);userdir.chmod(0o700)
 # A previous dedicated instance for this role may still hold the port right
 # after its worker exited (SIGTERM delivery + teardown lag). Terminate that
 # exact process group, verify process exit, and wait for port release
 # before probing.
 if not port_free(port):release_own_instance(port,userdir)
 # Never attach to an unknown process on the expected port. SO_REUSEADDR
 # matches the port_free probe: server-side TIME_WAIT left by closed CDP
 # connections must not be mistaken for a live listener, while a real
 # listener still refuses the bind.
 probe=socket.socket()
 try:probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);probe.bind(('127.0.0.1',port))
 except OSError:raise RuntimeError(f'Dedicated browser port {port} is still occupied after teardown wait; refusing unknown session')
 finally:probe.close()
 cfgpath=profile_charter(role).parents[2]/'config.yaml';cfg=yaml.safe_load(cfgpath.read_text());cfg['browser']['cdp_url']=f'http://127.0.0.1:{port}';cfgpath.write_text(yaml.safe_dump(cfg))
 log=(Path(run)/'reports'/f'chrome-{role}.log').open('a')
 p=subprocess.Popen([CHROME,f'--remote-debugging-port={port}','--remote-debugging-address=127.0.0.1',f'--user-data-dir={userdir}','--no-first-run','--no-default-browser-check','about:blank'],stdout=log,stderr=subprocess.STDOUT,start_new_session=True);log.close()
 for _ in range(50):
  if p.poll() is not None:raise RuntimeError('Dedicated Chrome exited; see Chrome log')
  try:
   with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version',timeout=1) as r:
    if json.load(r).get('webSocketDebuggerUrl'):return p
  except Exception:time.sleep(.2)
 p.terminate();raise RuntimeError('Dedicated Chrome failed preflight')
def port_holders(port):
 # Diagnostics only: which processes hold this TCP port (listener or not).
 try:
  out=subprocess.run(['/usr/sbin/lsof','-nP','-iTCP:'+str(port)],capture_output=True,text=True,timeout=10)
  lines=[l for l in out.stdout.splitlines() if l and not l.startswith('COMMAND')]
  return [l.split()[1]+':'+l.split()[8]+' '+(' '.join(l.split()[9:])[:60]) for l in lines if len(l.split())>8][:8]
 except Exception as e:return ['lsof failed: '+str(e)[:60]]
def stop(p,port=None):
 # P2 lifecycle contract: terminate the owned process group, VERIFY process
 # exit (helpers can outlive a killed launcher and keep the port bound),
 # then VERIFY the CDP port is released — and fail loudly if it never is,
 # so the next same-role dispatch refuses the port instead of colliding.
 dbg=os.environ.get('CHROME_TEARDOWN_DEBUG')
 def trace(msg):
  if dbg:
   try:(Path(dbg)).open('a').write(f'{time.strftime("%H:%M:%S")} {msg}\n')
   except Exception:pass
 if p is None:return
 trace(f'stop pid={p.pid} port={port} poll={p.poll()}')
 if p.poll() is None:
  try:os.killpg(p.pid,signal.SIGTERM);trace(f'SIGTERM sent to group {p.pid}')
  except (ProcessLookupError,PermissionError):pass
  try:p.wait(timeout=5);trace(f'launcher wait returned rc={p.poll()}')
  except subprocess.TimeoutExpired:
   # SIGKILL the launcher's own group only (helpers share pgid==pid); a
   # bare p.kill() would orphan the helpers and is exactly the pre-P2 bug.
   trace('launcher wait TIMED OUT; escalating SIGKILL')
   try:os.killpg(p.pid,signal.SIGKILL);trace(f'SIGKILL sent to group {p.pid}')
   except (ProcessLookupError,PermissionError):pass
   p.wait()
 if port is not None:
  userdir=userdir_for_port(port)
  if userdir is not None:
   if not wait_own_exit(userdir,time.monotonic()+GROUP_EXIT_WAIT_SECONDS):
    signal_own_groups(own_instance_pids(userdir),signal.SIGKILL) # owned groups only
    wait_own_exit(userdir,time.monotonic()+GROUP_EXIT_WAIT_SECONDS)
  deadline=time.monotonic()+PORT_WAIT_SECONDS
  while time.monotonic()<deadline and not port_free(port):time.sleep(.25)
  if not port_free(port):
   trace(f'port {port} STILL occupied; holders: {port_holders(port)}')
   raise RuntimeError(f'Dedicated browser role port {port} was not released after teardown; refusing to leave it for the next same-role dispatch')
