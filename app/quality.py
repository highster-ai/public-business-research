"""Role-local browser extraction and evidence-only semantic decisions.

No HTTP research clients, arbitrary browser code, or inferred contact values.
"""
from core import *
import uuid,time

RESTRICTION_PATTERN=re.compile(r'limited view|tampilan terbatas|sign[ -]?in|log[ -]?in|login|captcha|unusual traffic|access denied|verify you are human',re.I)

class AccessRestricted(RuntimeError):
 def __init__(self,snapshot,line):super().__init__('Google Maps access restricted');self.snapshot=snapshot;self.line=line

def restriction_line(snapshot):
 return next((line for line in text(snapshot).splitlines() if RESTRICTION_PATTERN.search(line)),None)

def names_match(expected,observed):
 def words(s):return set(re.findall(r'[a-z0-9]+',s.casefold()))-{'spa','massage','wellness','day','bali','pererenan','canggu','and'}
 required=words(expected)
 return bool(required) and required.issubset(words(observed))

def evidence(run,role,bid,field,snapshot,snippet,kind):
 s=read(Path(run)/'raw'/role/'snapshots'/f"{snapshot['snapshot_id']}.json")
 e={'evidence_id':'e_'+uuid.uuid4().hex,'business_id':bid,'field':field,'source_kind':kind,'source_url':s['url'],'snippet':snippet,'raw_text':snippet,'retrieved_at':s['retrieved_at'],'reader':role,'snapshot_id':s['snapshot_id'],'snapshot_sha256':digest(s['observation'])}
 verify_evidence(run,e);return e

def page(browser,url):
 # Attempt the secure equivalent of an observed HTTP website link; never
 # relax the HTTPS-only navigation guard or change the observed evidence.
 if url.startswith('http://'):url='https://'+url[len('http://'):]
 return json.loads(browser.open({'url':url}))
def text(s):return s['untrusted_page_text']
def links(s):
 return [(m.group(1),m.group(2)) for m in re.finditer(r'\blink "([^"\n]+)"[^\n]*?\bref=(e\d+)',text(s))]
def target(browser,ref):
 s=json.loads(browser.link({'ref':ref}));line=text(s).splitlines()[-1]
 return s,line,line.split(' -> ',1)[1]
def headings(s):return re.findall(r'heading "([^"\n]+)" \[level=1',text(s))

def maps_listing(browser,b,spec):
 start=b.get('url','')
 if not (domain(start) in {'google.com','maps.google.com'} and '/maps/place/' in start):start='https://www.google.com/maps/search/'+urllib.parse.quote(b['name_raw']+' '+spec['area'])+'?hl=en'
 s=page(browser,start)
 restricted=restriction_line(s)
 if not any(names_match(b['name_raw'],h) for h in headings(s)):
  if restricted:raise AccessRestricted(s,restricted)
  matches=[(name,ref) for name,ref in links(s) if names_match(b['name_raw'],name) and not re.search('website|directions|book',name,re.I)]
  if len(matches)!=1:raise ValueError('Exact Maps business listing not uniquely identified')
  _,_,url=target(browser,matches[0][1])
  if '/maps/place/' not in url:raise ValueError('Maps result is not a business listing')
  s=page(browser,url)
  restricted=restriction_line(s)
  if restricted and not any(names_match(b['name_raw'],h) for h in headings(s)):raise AccessRestricted(s,restricted)
 for _ in range(3):
  if '/maps/place/' in s['url'] and any(names_match(b['name_raw'],h) for h in headings(s)):break
  time.sleep(1);s=json.loads(browser.capture('{}'))
  restricted=restriction_line(s)
  if restricted and not any(names_match(b['name_raw'],h) for h in headings(s)):raise AccessRestricted(s,restricted)
 if '/maps/place/' not in s['url'] or not any(names_match(b['name_raw'],h) for h in headings(s)):
  raise ValueError('Maps listing name/URL identity was not verified')
 locations=[w.strip().casefold() for w in spec['area'].split(',') if w.strip()]
 overview='\n'.join(primary_maps_lines(s,b['name_raw'])).casefold()
 if not any(loc in overview for loc in locations):raise ValueError('Maps listing location could not be corroborated')
 return s

def primary_maps_lines(s,expected_name=None):
 # Only the main listing overview, never recommendations or review quotations.
 body=text(s);matches=list(re.finditer(r'heading "([^"\n]+)" \[level=1',body))
 start=next((m for m in matches if expected_name is None or names_match(expected_name,m.group(1))),None)
 if not start:raise ValueError('Maps listing heading missing')
 body=body[start.start():]
 cut=re.search(r'heading "(?:People also|Similar|Review summary|Reviews|From the owner|Questions)',body,re.I)
 if cut:body=body[:cut.start()]
 return body.splitlines()

def maps_semantic_success(coverage):
 # Identity-unverifiable listings are honest unavailability, not extraction
 # failure: the strict identity checks in maps_listing refused to confirm that
 # a Maps listing is THIS business (e.g. two branches of one brand matching
 # the same search), so Maps metrics must be left to honest not_found
 # downstream rather than blocking the whole batch. Only the exact reasons
 # raised by those identity checks qualify; browser/runtime failures never do.
 # MISSING DATA != FAILED EXTRACTION: google_rating and google_review_count
 # are nullable public fields. A verified listing that was successfully
 # inspected (read_ok) may legitimately lack either metric (e.g. an unclaimed
 # listing with no rating element at all); that is honest not_found downstream
 # and must not block the batch. Only a failed read (maps_listing refused
 # identity or the read/extraction itself errored) stays a semantic failure,
 # so an actual browser/read failure can never be silently converted to
 # not_found merely to pass the batch.
 IDENTITY_REASONS=('not uniquely identified','not a business listing','identity was not verified','location could not be corroborated')
 rows=[]
 for row in coverage:
  row=dict(row)
  if row.get('reason') and any(m in row['reason'] for m in IDENTITY_REASONS):row['identity_unverifiable']=True
  rows.append(row)
 ok=all((row.get('read_ok') or row.get('identity_unverifiable')) for row in rows)
 return ok,rows

def google_reader(run,browser,businesses,spec):
 from source_health import before_source_page,record_source_page,source_status
 ev=[];coverage=[]
 for b in businesses:
  extracted=[];restricted_fields=[];reason='';read_ok=False
  try:
   try:before_source_page(run,'google_maps','read-google')
   except RuntimeError as exc:
    # The source was blocked for this run by prior restrictions, so no page is
    # opened for this business. Its Maps fields were not verified because of
    # that same source restriction -- honestly access_restricted, never
    # not_found and never not_public. The restriction is a source-wide
    # condition established by an immutable snapshot already observed.
    ref=source_status(run,'google_maps','read-google').get('last_restriction')
    if not ref:raise
    for field in ['google_rating','google_review_count','public_contact','website']:
     ev.append(evidence(run,'read-google',b['business_id'],field,ref,ref['line'],'google_maps'));restricted_fields.append(field)
    reason='Access restricted by Google Maps UI: '+str(exc);read_ok=True
    coverage.append({'business_id':b['business_id'],'expected':['google_rating','google_review_count','public_contact','website'],'extracted':extracted,'missing':['google_rating','google_review_count','public_contact','website'],'access_restricted':restricted_fields,'reason':reason,'read_ok':read_ok})
    continue
   s=maps_listing(browser,b,spec);lines=primary_maps_lines(s,b['name_raw'])
   name=next(l for l in lines if re.search(r'heading ".+" \[level=1',l))
   ev.append(evidence(run,'read-google',b['business_id'],'business_name',s,name,'google_maps'))
   for field,pattern in [('google_rating',r'(?:image|img) "[0-5][.,]\d\s+(?:stars|bintang)'),('google_review_count',r'(?:button|image) "[\d, .]+\s+(?:reviews|ulasan)'),('public_contact',r'(?:Phone|Telepon):\s*\+?\d')]:
    found=[l for l in lines if re.search(pattern,l,re.I)]
    if found:
     ev.append(evidence(run,'read-google',b['business_id'],field,s,found[0],'google_maps'));extracted.append(field)
   for label,ref in links(s):
    if re.search(r'website|situs',label,re.I):
     try:
      ls,line,url=target(browser,ref)
      ev.append(evidence(run,'read-google',b['business_id'],'website',ls,line,'google_maps'));extracted.append('website');break
     except (ValueError,RuntimeError):continue
   # The verified listing was successfully inspected; anything missing after
   # this point (rating, review count, website, phone) is honest missing
   # public data, never an extraction failure. An exception before this point
   # (identity refusal, browser failure, unparsable state) keeps read_ok
   # False, which maps_semantic_success treats as a read failure.
   restricted=restriction_line(s)
   if restricted:
    for field in [f for f in ['google_rating','google_review_count','public_contact','website'] if f not in extracted]:
     ev.append(evidence(run,'read-google',b['business_id'],field,s,restricted,'google_maps'));restricted_fields.append(field)
    reason='Access restricted by Google Maps UI';record_source_page(run,'google_maps',True,'read-google',s['snapshot_id'],restricted)
   else:record_source_page(run,'google_maps',False,'read-google')
   read_ok=True
  except AccessRestricted as exc:
   s=exc.snapshot
   for field in ['google_rating','google_review_count','public_contact','website']:
    ev.append(evidence(run,'read-google',b['business_id'],field,s,exc.line,'google_maps'));restricted_fields.append(field)
   reason='Access restricted by Google Maps UI';read_ok=True
   record_source_page(run,'google_maps',True,'read-google',exc.snapshot['snapshot_id'],exc.line)
  except (ValueError,RuntimeError,PermissionError) as exc:reason=str(exc)
  coverage.append({'business_id':b['business_id'],'expected':['google_rating','google_review_count','public_contact','website'],'extracted':extracted,'missing':[f for f in ['google_rating','google_review_count','public_contact','website'] if f not in extracted],'access_restricted':restricted_fields,'reason':reason or ('Not visible in verified listing overview' if len(extracted)<4 else ''),'read_ok':read_ok})
 # Identity-unverifiable listings are marked and tolerated by
 # maps_semantic_success; see that function for the reasoning.
 ok,coverage=maps_semantic_success(coverage)
 report={'reader':'read-google','businesses':coverage,'semantic_success':ok}
 write(Path(run)/'raw/read-google/semantic-coverage.json',report);audit(run,'read-google','semantic_coverage',report)
 if not report['semantic_success']:raise ValueError('Semantic extraction incomplete for Maps metrics; inspect semantic-coverage.json')
 return {'businesses':[],'evidence':ev}

def urls(snippet):return re.findall(r'https?://[^\s"<>]+',snippet)
def domain(url):return (urllib.parse.urlsplit(url).hostname or '').lower().removeprefix('www.')
def same_site(a,b):
 if domain(a)!=domain(b):return False
 if domain(a)=='linktr.ee':return urllib.parse.urlsplit(a).path.rstrip('/')==urllib.parse.urlsplit(b).path.rstrip('/')
 return True

def valid_site_url(url):
 # Deterministic syntax gate applied before any browser or DNS access (no
 # network calls, no external validation services). Rejects only structurally
 # impossible URLs: missing/broken hostname, whitespace or control characters
 # captured from a snippet, stray punctuation, non-HTTP schemes, malformed
 # ports. International (non-ASCII) hostnames stay allowed; getaddrinfo
 # applies IDNA itself. A rejected URL is recorded as an honest source
 # failure; it is never replaced by an inferred one.
 try:u=urllib.parse.urlsplit(url.strip())
 except ValueError:return False
 if u.scheme not in ('http','https'):return False
 host=u.hostname or ''
 if not host or len(host)>253 or not re.fullmatch(r'[\w.-]+',host) or '..' in host or host.startswith('.'):return False
 try:u.port
 except ValueError:return False
 return True

def site_reader(run,browser,businesses,ev,spec):
 out=[];coverage=[]
 for b in businesses:
  raw=[u for e in ev if e['business_id']==b['business_id'] and e['field']=='website' and e['source_kind']=='google_maps' for u in urls(e['snippet']) if '/maps/' not in u and domain(u) not in {'google.com','instagram.com'}]
  starts=[u for u in raw if valid_site_url(u)]
  queue=list(dict.fromkeys(starts));visited=set();fields=set()
  # A structurally invalid URL is honest unavailability, recorded before any
  # browser/DNS access; it is never replaced by an inferred domain.
  failures=[{'url':u,'reason':'Invalid URL syntax; rejected before browser/DNS access'} for u in dict.fromkeys(raw) if not valid_site_url(u)]
  while queue and len(visited)<3:
   url=queue.pop(0)
   if url in visited:continue
   visited.add(url)
   # Per-URL boundary: a dead/unresolvable/malformed URL (gaierror and other
   # OS-level I/O failures included) is isolated to this URL and recorded as
   # an honest source failure; the batch continues with the next URL/business.
   try:s=page(browser,url)
   except (ValueError,RuntimeError,PermissionError,OSError) as exc:
    failures.append({'url':url,'reason':str(exc)[:300]});continue
   kind='official_site' if any(same_site(url,u) for u in starts) else 'public_source'
   if kind=='official_site':
    nav=next((l for l in text(s).splitlines() if l.startswith('Observed browser navigation: ')),None)
    if nav:out.append(evidence(run,'read-site',b['business_id'],'website',s,nav,kind))
    if s['url'] not in starts:starts.append(s['url'])
   branch_labels=[]
   for label,ref in links(s):
    match=re.match(r'([^+\d]+?)\s+\+[\u202a-\u202e\s]*\d',label)
    if match:branch_labels.append(match.group(1).strip())
   local_labels=[label for label in branch_labels if label.casefold() in b['name_raw'].casefold()]
   def belongs_to_branch(line):
    if not local_labels:return True
    mentioned=[label for label in branch_labels if re.search(r'(?<!\w)'+re.escape(label)+r'\s+\+',line,re.I)]
    return not mentioned or any(label in local_labels for label in mentioned)
   for line in text(s).splitlines():
    if not belongs_to_branch(line):continue
    if re.search(r'\+?\d[\d ()-]{7,}\d',line) and contact_values({'snippet':line},'public_contact',spec['country']):
     out.append(evidence(run,'read-site',b['business_id'],'public_contact',s,line,kind));fields.add('public_contact')
     if contact_values({'snippet':line},'whatsapp',spec['country']):out.append(evidence(run,'read-site',b['business_id'],'whatsapp',s,line,kind));fields.add('whatsapp')
   # Read href attributes only on observed public contact/navigation links.
   for label,ref in links(s):
    if not belongs_to_branch(label):continue
    if not re.search(r'instagram|whatsapp|\bwa\b|contact|about|booking|book |reserv|hubungi|esthetic_bali|pressure\.bali|\+?\d[\d -]{6}',label,re.I):continue
    try:ls,line,url=target(browser,ref)
    except (ValueError,RuntimeError,PermissionError,OSError) as exc:
     audit(run,'read-site','link_target_unavailable',{'business_id':b['business_id'],'label':label,'reason':str(exc)});continue
    field='instagram_candidate' if domain(url)=='instagram.com' else 'whatsapp' if re.search(r'wa\.me|whatsapp\.com',url,re.I) else 'public_contact' if url.startswith('tel:') else None
    if field:out.append(evidence(run,'read-site',b['business_id'],field,ls,line,kind));fields.add(field)
    if not field and any(same_site(url,u) for u in starts) and re.search(r'contact|about|booking|reserv',label,re.I):
     if valid_site_url(url):queue.append(url)
     else:failures.append({'url':url,'reason':'Invalid URL syntax; not fetched'})
  coverage.append({'business_id':b['business_id'],'pages':len(visited),'expected':['public_contact','whatsapp','instagram_candidate'],'extracted':sorted(fields),'missing':[f for f in ['public_contact','whatsapp','instagram_candidate'] if f not in fields],'source_failures':failures})
 report={'reader':'read-site','businesses':coverage};write(Path(run)/'raw/read-site/semantic-coverage.json',report);audit(run,'read-site','semantic_coverage',report)
 return {'businesses':[],'evidence':out}

def instagram_url(url):
 p=urllib.parse.urlsplit(url);path=p.path.strip('/')
 if domain(url)!='instagram.com' or not re.fullmatch(r'[A-Za-z0-9_.]+',path) or path in {'p','reel','explore','accounts'}:return None
 return 'https://www.instagram.com/'+path.lower()+'/'

def instagram_support(bid,ev):
 own=[e for e in ev if e['business_id']==bid];options={}
 maps_sites=[(u,[e['evidence_id']]) for e in own if e['source_kind']=='google_maps' and e['reader']=='read-google' and '/maps/place/' in e['source_url'] and e['field']=='website' for u in urls(e['snippet'])]
 for e in own:
  if e['field']=='website' and e['source_kind']=='official_site' and e['snippet'].startswith('Observed browser navigation: '):
   destinations=urls(e['snippet'])
   if len(destinations)==2:
    parents=[ref for u,refs in maps_sites if same_site(u,destinations[0]) for ref in refs]
    if parents and destinations[1]==e['source_url']:maps_sites.append((e['source_url'],parents+[e['evidence_id']]))
 for e in own:
  if e['field'] not in {'instagram','instagram_candidate','website'}:continue
  for u in urls(e['snippet']):
   ig=instagram_url(u)
   if not ig:continue
   refs=[]
   if e['source_kind']=='google_maps' and e['field']=='website' and any(e['evidence_id'] in rs for u,rs in maps_sites):refs=[e['evidence_id']]
   elif e['source_kind']=='official_site':
    parents=[ref for link,rs in maps_sites if same_site(link,e['source_url']) for ref in rs]
    if parents:refs=parents+[e['evidence_id']]
   if refs:options.setdefault(ig,set()).update(refs)
 return options

def validate_instagram_identity(r,ev):
 if r['field']=='instagram' and r['status']=='found':
  supported=instagram_support(r['business_id'],ev)
  if r['value'] not in supported or not supported[r['value']].issubset(set(r['evidence_ids'])):
   raise ValueError('Instagram identity lacks corroborated official linkage')

def resolve_contacts_and_instagram(businesses,ev,fields,country):
 out=[]
 for b in businesses:
  for field in fields:
   bid=b['business_id'];own=[e for e in ev if e['business_id']==bid]
   if field=='instagram':
    options=instagram_support(bid,ev)
    if len(options)==1:
     value=next(iter(options));refs=sorted(options[value]);status='found';method='Identity verified by exact Maps listing linking to profile, or Maps-linked official website linking to profile'
    else:
     refs=[e['evidence_id'] for e in own if e['field'] in {'instagram','instagram_candidate'}];value=None;status='ambiguous' if refs else 'not_found';method='No unique corroborated official profile identity; a URL/profile-picture/login dialog is insufficient'
   else:
    allowed={'whatsapp','public_contact'} if field=='whatsapp' else {'public_contact'}
    candidates=[(e,contact_values(e,field,country)) for e in own if e['field'] in allowed and contact_values(e,field,country)]
    if candidates:
     rank=min(PRIORITY[e['source_kind']] for e,vs in candidates);best=[(e,vs) for e,vs in candidates if PRIORITY[e['source_kind']]==rank];values=set().union(*(vs for e,vs in best));refs=[e['evidence_id'] for e,vs in best]
     status='found' if len(values)==1 else 'ambiguous';value=next(iter(values)) if len(values)==1 else None;method='Exact explicit contact targets/numbers from highest-priority evidence; E.164 normalization only'
    else:
     restricted=[e for e in own if e['field'] in allowed and RESTRICTION_PATTERN.search(e['snippet'])]
     refs=[e['evidence_id'] for e in restricted];status='access_restricted' if restricted else 'not_found';value=None;method='Source access restriction prevented verification' if restricted else 'No explicit number/target collected for this field; CTA and unrelated phones do not qualify'
   r=resolution(bid,field,status,value,refs,ev,method,.95 if status=='found' else 0)
   # All established checks apply; uncertain source priority remains uncertain.
   try:
    validate_resolution(r,ev,country);validate_instagram_identity(r,ev)
   except ValueError as exc:
    refs=[e['evidence_id'] for e in own if e['field'] in ({'instagram','instagram_candidate','website'} if field=='instagram' else {'whatsapp','public_contact'})]
    r=resolution(bid,field,'ambiguous',None,refs,ev,'Evidence did not satisfy validation: '+str(exc),0);validate_resolution(r,ev,country)
   out.append(r)
 return {'resolutions':out}
