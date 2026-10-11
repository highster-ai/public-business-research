from core import *
import sys

def role_tmp(run,role):
 # The one temporary directory a role may read and write: per-run and per-role,
 # and inside the run, so scratch space sits within the role's own boundary. It
 # replaces the machine-wide /private/tmp, which every local process -- and
 # every OTHER role -- can read and write.
 return (Path(run).resolve()/'tmp'/role)

def policy(run,role):
 run=Path(run).resolve();profile=profile_charter(role).parents[2]
 reads=[ROOT/'app',ROOT/'schemas',ROOT/'profiles.json',ROOT/'model-policy.json',ROOT/'venv',Path(sys.base_prefix).resolve(),HERMES,profile,run/'control',run/'workspaces'/role,role_tmp(run,role)]
 if role.startswith('render-'):reads += [run/'canonical',run/'deliverables']
 elif role=='supervisor':reads += [run/'canonical',run/'reports']
 else:reads += [run/'raw',run/'resolved']
 writes=[profile,run/'control',run/'raw'/role,run/'resolved'/role,run/'workspaces'/role,run/'reports'/'failures',role_tmp(run,role)]
 if role=='consolidator':writes += [run/'canonical']
 if role=='supervisor':writes += [run/'reports']
 if role.startswith('render-'):writes += [run/'deliverables']
 browser=role in READERS
 if browser:reads += [Path.home()/'.npm',Path.home()/'.cache',Path.home()/'.hermes/node'];writes += [Path.home()/'.npm',Path.home()/'.cache']
 # Browser driver uses isolated temp user-data-dir, never normal Chrome cookies.
 rules=['(version 1)','(allow default)','(deny file-read-data (subpath '+json.dumps(str(Path.home()))+'))',
   # The machine-wide temporary directories are readable and writable by every
   # local process, so they are denied outright; a role's own temporary
   # directory is re-allowed below and nothing else there is. Two separate
   # reasons this is a denial and not merely the absence of an allowance: the
   # read boundary is otherwise expressed only as a denial under the home
   # directory, so a run that happened to live under /tmp made every path in
   # the run -- canonical output, another role's raw evidence, another
   # process's scratch files -- readable by default; and an explicit denial
   # here cannot be re-opened by an allow rule that names a wider path.
   '(deny file-read-data (subpath "/private/tmp"))','(deny file-read-data (subpath "/private/var/folders"))',
   # The run directory is denied too, and for the same reason: the read
   # boundary must not depend on where the run happens to live. Written as a
   # denial under the home directory alone, it covered a run inside the home
   # and left a run outside it -- under /tmp, say -- readable in full. The
   # role's own paths are re-allowed below, so this denies exactly the rest of
   # the run: other roles' directories, and canonical output for roles that do
   # not own it.
   '(deny file-read-data (subpath '+json.dumps(str(run))+'))',
   '(deny file-write*)']
 def filt(p):return '(subpath '+json.dumps(str(Path(p).resolve()))+')'
 rules += ['(allow file-read-data '+ ' '.join(filt(p) for p in reads)+')']
 # No blanket access to /private/tmp or /private/var/folders. Both are
 # machine-wide: any local process can read and write there, so allowing them
 # let a role read a home placed below /tmp, read and write another role's
 # directories whenever the run itself lived under /tmp, and touch unrelated
 # files belonging to other processes. The role's own temporary directory --
 # allowed above, and named to the worker through TMPDIR -- replaces them.
 # Only two literal system paths survive, because the macOS runtime opens them
 # and neither exposes file data: /dev/null, and the dtrace helper device.
 rules += ['(allow file-write* '+ ' '.join(filt(p) for p in writes)+' (literal "/dev/null") (literal "/dev/dtracehelper"))']
 # The denials below come last on purpose: sandbox rules are last-match-wins,
 # so a role boundary written after an allow still holds even when the run
 # directory itself sits under /tmp.
 if role!='consolidator':rules += ['(deny file-write* (subpath '+json.dumps(str(run/'canonical'))+'))']
 if role.startswith('render-'):rules += ['(deny file-read-data (subpath '+json.dumps(str(run/'raw'))+'))']
 # Audit is role-specific. Renaming atomic writes needs parent audit-dir access.
 rules += ['(allow file-read-data file-write* (literal '+json.dumps(str(run/'audit'))+') (literal '+json.dumps(str(run/'audit'/f'{role}.jsonl'))+'))']
 rules += ['(deny file-write* (literal '+json.dumps(str(profile/'config.yaml'))+') (subpath '+json.dumps(str(profile/'skills'))+'))']
 if not browser:rules += ['(deny network-outbound)','(allow network-outbound (remote ip "localhost:11434"))']
 # Jev SHADOW (experimental, flag-gated): permit writes to the isolated
 # shadow/ directory and one HTTPS egress host for the two shadowed resolver
 # roles. With JEV_SHADOW_ENABLED unset this block is skipped entirely and
 # the policy is byte-identical to the pre-shadow policy.
 if os.environ.get('JEV_SHADOW_ENABLED')=='true' and role in {'resolve-owner','resolve-dm'}:
  rules += ['(allow file-read-data file-write* (subpath '+json.dumps(str(run/'shadow'))+'))',
            '(allow network-outbound (remote ip "api.typesafe.ai:443"))']
 # Jev LIVE A/B experiment (M4, flag-gated): identical isolation pattern to the
 # shadow block — isolated <run>/shadow writes and ONE HTTPS egress host, only
 # for the two experimental roles (resolve-owner screening M1, find-maps Maps
 # triage M2), only while JEV_AB_ENABLED is set. With the flag unset this
 # block is skipped entirely and the policy is byte-identical to the pre-
 # experiment policy.
 if os.environ.get('JEV_AB_ENABLED')=='true' and role in {'resolve-owner','find-maps'}:
  rules += ['(allow file-read-data file-write* (subpath '+json.dumps(str(run/'shadow'))+'))',
            '(allow network-outbound (remote ip "api.typesafe.ai:443"))']
 return '\n'.join(rules)+'\n'
