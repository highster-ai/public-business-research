"""Run-local health accounting for browser data sources.

Worker roles run inside the fail-closed Guard (app/guard.py), which allows
writes only under the role's own `raw/<role>/` staging. Source-health
observations are therefore staged role-locally here and merged by the host
into the host-owned run-level `reports/source-health.json`; a worker never
writes run-level reports.
"""
from core import Path, os, read, write, now
import time

SOURCE_RESTRICTION_LIMIT = int(os.environ.get('LEADGEN_SOURCE_RESTRICTION_LIMIT', '3'))
SOURCE_RESTRICTION_BACKOFF_SECONDS = float(os.environ.get('LEADGEN_SOURCE_RESTRICTION_BACKOFF_SECONDS', '2'))

def _run_path(run):
 return Path(run)/'reports/source-health.json'

def _role_path(run,role):
 return Path(run)/'raw'/role/'source-health.json'

def load_source_health(run):
 '''Host-published run-level view (merged from role-local staging).'''
 p=_run_path(run)
 return read(p) if p.exists() else {}

def _combine(a,b):
 '''Aggregate two observations of the same source. Each page open is one
 observation, so counts add; severity is the most restrictive of the two.'''
 m=dict(a)
 m['pages_ok']=a.get('pages_ok',0)+b.get('pages_ok',0)
 m['pages_restricted']=a.get('pages_restricted',0)+b.get('pages_restricted',0)
 m['consecutive_restrictions']=max(a.get('consecutive_restrictions',0),b.get('consecutive_restrictions',0))
 m['blocked']=bool(a.get('blocked')) or bool(b.get('blocked'))
 order={'healthy':0,'degraded':1,'blocked':2}
 status=max([a.get('status','healthy'),b.get('status','healthy')],key=lambda s:order.get(s,1))
 m['status']='blocked' if m['blocked'] else status
 if m['blocked']:m['blocked_at']=a.get('blocked_at') or b.get('blocked_at')
 if b.get('last_restriction') and b.get('pages_restricted',0)>=a.get('pages_restricted',0):m['last_restriction']=b['last_restriction']
 m['restriction_limit']=b.get('restriction_limit',a.get('restriction_limit',SOURCE_RESTRICTION_LIMIT))
 stamps=[x for x in (a.get('updated_at'),b.get('updated_at')) if x]
 m['updated_at']=max(stamps) if stamps else now()
 return m

def merge_source_health(run):
 '''Host-side: fold every role-local staging file into the run-level report.'''
 merged={}
 for path in sorted((Path(run)/'raw').glob('*/source-health.json')):
  for source,state in read(path).items():
   merged[source]=_combine(merged[source],state) if source in merged else state
 write(_run_path(run),merged)
 return merged

def source_status(run,source,role):
 p=_role_path(run,role)
 return (read(p) if p.exists() else {}).get(source,{'status':'healthy','pages_ok':0,'pages_restricted':0,'blocked':False,'consecutive_restrictions':0})

def before_source_page(run,source,role):
 state=source_status(run,source,role)
 if state.get('blocked'):raise RuntimeError(f'Source blocked for this run after repeated access restrictions: {source}')
 if state.get('status')=='degraded' and SOURCE_RESTRICTION_BACKOFF_SECONDS>0:
  time.sleep(SOURCE_RESTRICTION_BACKOFF_SECONDS)
 return state

def record_source_page(run,source,restricted,role,snapshot_id=None,line=None):
 p=_role_path(run,role);health=read(p) if p.exists() else {};state=source_status(run,source,role)
 if restricted:
  state['pages_restricted']=state.get('pages_restricted',0)+1
  state['consecutive_restrictions']=state.get('consecutive_restrictions',0)+1
  state['status']='degraded'
  # The observed restriction is source-wide, not business-specific: remember
  # the immutable snapshot that established it so later businesses whose pages
  # are never opened (source blocked) can still be recorded honestly as
  # access_restricted rather than not_found.
  if snapshot_id:state['last_restriction']={'snapshot_id':snapshot_id,'line':line or ''}
  if state['consecutive_restrictions']>=SOURCE_RESTRICTION_LIMIT:
   state['blocked']=True;state['status']='blocked';state['blocked_at']=now()
 else:
  state['pages_ok']=state.get('pages_ok',0)+1
  state['consecutive_restrictions']=0
  if not state.get('blocked'):state['status']='degraded' if state.get('pages_restricted',0) else 'healthy'
 state['restriction_limit']=SOURCE_RESTRICTION_LIMIT;state['updated_at']=now()
 health[source]=state;write(p,health)
 return state
