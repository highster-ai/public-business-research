"""Fail-closed worker execution backends.

Local keeps the existing macOS sandbox. Docker starts one disposable container
per claimed task; no Docker socket, host home, or credential directory is
mounted into the worker.
"""
from core import *
import fcntl, shutil, subprocess

BACKEND_ENV='LEADGEN_EXECUTION_BACKEND'
IMAGE_ENV='LEADGEN_DOCKER_IMAGE'
ALLOWED_BACKENDS={'local','docker'}
DEFAULT_IMAGE='leadgen-worker:local'
DOCKER_LIMITS={'pids':256,'memory':'5g','cpus':2}

def backend(explicit=None):
 value=(explicit if explicit is not None else os.environ.get(BACKEND_ENV,'docker')).strip().lower()
 if value not in ALLOWED_BACKENDS:raise ValueError(f'{BACKEND_ENV} must be local or docker')
 return value

def backend_was_explicit(explicit=None):
 return explicit is not None or BACKEND_ENV in os.environ

def docker_available():
 binary=shutil.which('docker')
 if not binary:return False
 try:return subprocess.run([binary,'info'],capture_output=True,timeout=15).returncode==0
 except (OSError,subprocess.TimeoutExpired):return False

def docker_details(image=None):
 """Return the immutable image identity, failing closed on every Docker error."""
 binary=shutil.which('docker')
 if not binary:raise RuntimeError('Docker executable is unavailable')
 try:
  info=subprocess.run([binary,'info'],capture_output=True,text=True,timeout=15)
 except (OSError,subprocess.TimeoutExpired) as exc:raise RuntimeError('Docker daemon is unavailable') from exc
 if info.returncode:raise RuntimeError('Docker daemon is unavailable: '+(info.stderr or info.stdout)[-500:].strip())
 image=image or os.environ.get(IMAGE_ENV,DEFAULT_IMAGE)
 try:
  inspected=subprocess.run([binary,'image','inspect',image,'--format','{{.Id}}'],capture_output=True,text=True,timeout=15)
 except (OSError,subprocess.TimeoutExpired) as exc:raise RuntimeError('Docker image inspection failed for '+image) from exc
 if inspected.returncode or not inspected.stdout.strip():raise RuntimeError('Docker image is unavailable: '+image)
 return {'backend':'docker','image':image,'image_digest':inspected.stdout.strip(),'limits':dict(DOCKER_LIMITS)}

def _lock_is_free(path):
 path=Path(path)
 if not path.exists():return True
 try:
  with path.open('a+') as handle:
   fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
   fcntl.flock(handle,fcntl.LOCK_UN)
  return True
 except (OSError,BlockingIOError):return False

def local_details():
 """Check the local Hermes installation before a host worker may spawn."""
 if not HERMES.is_dir():raise RuntimeError('Hermes installation is unavailable: '+str(HERMES))
 try:
  from hermes_cli import update_lock
  if update_lock.update_in_progress(HERMES):raise RuntimeError('Hermes update lock is active')
 except RuntimeError:raise
 except Exception as exc:raise RuntimeError('Hermes update-lock health check failed: '+str(exc)) from exc
 recovery=HERMES.parent/'state'/'.recovery.lock'
 if not _lock_is_free(recovery):raise RuntimeError('Hermes recovery lock is active: '+str(recovery))
 hermes=shutil.which('hermes')
 if not hermes:raise RuntimeError('Hermes CLI is unavailable')
 try:probe=subprocess.run([hermes,'--version'],capture_output=True,text=True,timeout=15)
 except (OSError,subprocess.TimeoutExpired) as exc:raise RuntimeError('Hermes health check failed') from exc
 if probe.returncode:raise RuntimeError('Hermes health check failed: '+(probe.stderr or probe.stdout)[-500:].strip())
 return {'backend':'local','image':None,'image_digest':None,'limits':{'sandbox':'macos','task_timeouts':'stage-specific'}}

def runtime_details(selected=None,image=None):
 return docker_details(image) if backend(selected)=='docker' else local_details()

def docker_command(run,key,role):
 run=Path(run).resolve();root=ROOT.resolve();charter=profile_charter(role)
 image=os.environ.get(IMAGE_ENV,DEFAULT_IMAGE)
 name=('leadgen-'+key).lower().replace('_','-')[:63]
 cmd=['docker','run','--rm','--name',name,'--init','--cap-drop=ALL',
      '--security-opt=no-new-privileges',f'--pids-limit={DOCKER_LIMITS["pids"]}',f'--memory={DOCKER_LIMITS["memory"]}',f'--cpus={DOCKER_LIMITS["cpus"]}',
      '--add-host=host.docker.internal:host-gateway',
      '--mount',f'type=bind,src={run},dst={run}',
      '--mount',f'type=bind,src={charter},dst=/opt/leadgen/charter.md,readonly',
      '-e','LEADGEN_ROOT=/opt/leadgen','-e','LEADGEN_CONTAINER=1','-e',f'LEADGEN_ROLE={role}',
      '-e','LEADGEN_HERMES_ROOT=/opt/hermes',
      '-e','LEADGEN_CHARTER=/opt/leadgen/charter.md','-e','HERMES_HOME=/tmp/hermes',
      '-e','LEADGEN_MODEL_BASE_URL=http://host.docker.internal:11434/v1']
 if role.startswith('render-') and role!='render-md':
  fmt=role.split('-',1)[1];skill_dir=charter.parents[1]/fmt
  cmd += ['--mount',f'type=bind,src={skill_dir},dst={skill_dir},readonly','-e',f'LEADGEN_RENDER_BUNDLE={skill_dir}']
 # Experimental API credentials remain explicit opt-ins, never broad env forwarding.
 for env_name in ['JEV_SHADOW_ENABLED','JEV_SHADOW_OWNER','JEV_SHADOW_REVIEW_REPLY','JEV_SHADOW_DIR','TYPESAFE_API_KEY','JEV_AB_ENABLED','JEV_AB_OWNER','JEV_AB_MAPS','JEV_AB_OWNER_T','JEV_AB_MAPS_T','JEV_AB_DIR']:
  if env_name in os.environ:cmd += ['-e',env_name]
 return cmd+[image,str(run),key]
