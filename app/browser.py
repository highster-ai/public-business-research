"""Only visible-page operations. No evaluate, request, fetch, CDP commands or outreach."""
from core import *
import uuid
class Browser:
 def __init__(self,run,role):
  if role not in READERS:raise PermissionError('Role has no browser capability')
  self.run=Path(run);self.role=role;self.session='leadgen-'+role+'-'+uuid.uuid4().hex[:10];self.url=None;self.opens=0;self.blocked=False;self.snapshots=[]
 def capture(self,result):
  from tools.browser_tool import browser_snapshot
  snap=browser_snapshot(full=True,task_id=self.session)
  # Hermes returns serialized browser observations. Preserve exact bytes as text.
  text=snap if isinstance(snap,str) else json.dumps(snap,ensure_ascii=False)
  nav=result if isinstance(result,str) else json.dumps(result,ensure_ascii=False)
  for raw in [nav,text]:
   try:
    v=json.loads(raw)
    if v.get('error') or v.get('success') is False:raise RuntimeError(str(v)[:700])
    actual=v.get('url') or (v.get('data',{}).get('url') if isinstance(v.get('data'),dict) else None)
    if actual:safe_url(actual,self.role);self.url=actual
   except json.JSONDecodeError:pass
  try:from tools.browser_tool_session import _run_browser_command
  except ImportError:from tools.browser_tool import _run_browser_command
  current=_run_browser_command(self.session,'get',['url'])
  if not current.get('success'):raise RuntimeError('Cannot verify current browser URL')
  self.url=current['data']['url'];safe_url(self.url,self.role)
  parts=[]
  for raw in [text]:
   try:
    parsed=json.loads(raw);parts.append(parsed.get('snapshot') or raw)
   except json.JSONDecodeError:parts.append(raw)
  observation='\n'.join(parts)
  observation+='\nObserved browser navigation: '+getattr(self,'requested_url',self.url)+' -> '+self.url
  if '/sorry/' in self.url or 'unusual traffic' in observation.lower():self.blocked=True
  return self.store(observation)
 def store(self,observation):
  sid='s_'+uuid.uuid4().hex
  record={'snapshot_id':sid,'url':self.url,'retrieved_at':now(),'reader':self.role,'observation':observation}
  write(self.run/'raw'/self.role/'snapshots'/f'{sid}.json',record,immutable=True)
  self.snapshots.append(sid)
  audit(self.run,self.role,'browser_snapshot',{'snapshot_id':sid,'url':self.url,'sha256':digest(observation)})
  return json.dumps({'snapshot_id':sid,'url':self.url,'untrusted_page_text':observation},ensure_ascii=False)
 def link(self,args,**kwargs):
  # Fixed read-only DOM attribute operation on an observed link, never eval or
  # a request to its target. Even WhatsApp links are only inspected, not opened.
  try:from tools.browser_tool_session import _run_browser_command
  except ImportError:from tools.browser_tool import _run_browser_command
  ref=args['ref']
  if not re.fullmatch(r'e\d+',ref) or not self.snapshots:raise ValueError('Observed link reference required')
  previous=read(self.run/'raw'/self.role/'snapshots'/f'{self.snapshots[-1]}.json')
  lines=[l.strip() for l in previous['observation'].splitlines() if re.search(r'\blink "',l) and re.search(r'\bref='+ref+r'\b',l)]
  if not lines:raise ValueError('Reference is not an observed link')
  result=_run_browser_command(self.session,'get',['attr','@'+ref,'href'])
  if not result.get('success'):raise RuntimeError('Could not read link target')
  data=result.get('data',{});target=data.get('value')
  if not isinstance(target,str) or not target:raise ValueError('Link has no target')
  origin=data.get('origin')
  if origin!=self.url:
   if kwargs.get('_refresh') is False:raise ValueError('Page changed while inspecting link')
   label=re.search(r'link "([^"\n]+)"',lines[0]).group(1)
   refreshed=json.loads(self.capture('{}'))
   refs=re.findall(r'\blink "'+re.escape(label)+r'"[^\n]*?\bref=(e\d+)',refreshed['untrusted_page_text'])
   if len(refs)!=1:raise ValueError('Link no longer uniquely observed after navigation')
   return self.link({'ref':refs[0]},_refresh=False)
  target=urllib.parse.urljoin(self.url,target)
  if urllib.parse.urlsplit(target).scheme not in {'https','http','tel','mailto'}:raise ValueError('Non-public link scheme')
  return self.store(previous['observation']+'\nObserved link target: '+lines[0]+' -> '+target)
 def open(self,args,**kwargs):
  from tools.browser_tool import browser_navigate
  if self.blocked:raise PermissionError('Browser access challenge: stop this source; no bypass')
  self.opens+=1
  if self.opens>75:raise PermissionError('Browser page budget exhausted')
  safe_url(args['url'],self.role);self.url=args['url'];self.requested_url=args['url']
  return self.capture(browser_navigate(self.url,task_id=self.session))
 def scroll(self,args,**kwargs):
  from tools.browser_tool import browser_scroll
  if not self.url:raise ValueError('Open a page first')
  if args['direction'] not in ('up','down'):raise ValueError('Invalid scroll direction')
  return self.capture(browser_scroll(args['direction'],task_id=self.session))
 def close(self):
  try:from tools.browser_tool_lifecycle import cleanup_browser
  except ImportError:from tools.browser_tool import cleanup_browser
  cleanup_browser(task_id=self.session)
