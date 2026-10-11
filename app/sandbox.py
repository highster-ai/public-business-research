from core import *
import sys

def policy(run,role):
 run=Path(run).resolve();profile=profile_charter(role).parents[2]
 reads=[ROOT/'app',ROOT/'schemas',ROOT/'profiles.json',ROOT/'model-policy.json',ROOT/'venv',Path(sys.base_prefix).resolve(),HERMES,profile,run/'control',run/'workspaces'/role]
 if role.startswith('render-'):reads += [run/'canonical',run/'deliverables']
 elif role=='supervisor':reads += [run/'canonical',run/'reports']
 else:reads += [run/'raw',run/'resolved']
 writes=[profile,run/'control',run/'raw'/role,run/'resolved'/role,run/'workspaces'/role,run/'reports'/'failures']
 if role=='consolidator':writes += [run/'canonical']
 if role=='supervisor':writes += [run/'reports']
 if role.startswith('render-'):writes += [run/'deliverables']
 browser=role in READERS
 if browser:reads += [Path.home()/'.npm',Path.home()/'.cache',Path.home()/'.hermes/node'];writes += [Path.home()/'.npm',Path.home()/'.cache']
 # Browser driver uses isolated temp user-data-dir, never normal Chrome cookies.
 rules=['(version 1)','(allow default)','(deny file-read-data (subpath '+json.dumps(str(Path.home()))+'))','(deny file-write*)']
 def filt(p):return '(subpath '+json.dumps(str(p))+')'
 rules += ['(allow file-read-data '+ ' '.join(filt(p) for p in reads)+')']
 rules += ['(allow file-write* '+ ' '.join(filt(p) for p in writes)+' (literal "/dev/null") (subpath "/private/tmp") (subpath "/private/var/folders"))']
 rules += ['(allow file-read-data (subpath "/private/tmp") (subpath "/private/var/folders"))']
 # Temporary-directory allowances support isolated test and CI worktrees, but
 # never broaden the role boundary for canonical data or renderer raw input.
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
