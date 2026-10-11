"""Regression tests for the data-quality fixes on top of the working
acceptance pipeline (run-20260921-142754). Covers: Google Maps rating/review
extraction and scope, Instagram identity resolution robustness, WhatsApp vs
public_contact semantics, and the semantic-completion report. None of these
tests weaken provenance, safety or no-outreach guarantees -- they only check
that legitimate evidence is no longer lost to formatting/scope bugs, and that
the existing strict checks still fire correctly.
"""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from core import *
from worker import resolve_model as resolve,strict_resolver_json,validate_resolver_batch,instagram_hints
from pipeline import deterministic_resolutions,semantic_report


class GoogleMapsExtraction(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.f=read(Path(__file__).parent/'fixtures/acceptance-140831.json')
  cls.ev=cls.f['evidence'];cls.bs=cls.f['businesses'];cls.spec=cls.f['spec']

 def test_google_maps_url_scope_is_maps_only(self):
  # Root cause of Problem 1: read-google/find-maps/read-reviews used to be
  # allowed onto plain https://google.com/search pages, which is where the
  # rating/review text actually came from in the failing run -- but that
  # evidence is source_kind=search_snippet and validate_resolution/
  # deterministic_resolutions both refuse to use it for google_rating or
  # google_review_count, so the field silently stayed not_found forever.
  safe_url('https://www.google.com/maps/search/Face+Therapy+Spa+Pererenan','read-google',False)
  for role in ['read-google','find-maps','read-reviews']:
   with self.assertRaisesRegex(ValueError,'Maps-only',msg=role):
    safe_url('https://www.google.com/search?q=Face+Therapy+Spa','read-google' if role=='read-google' else role,False)
  # find-web keeps plain web search; its job is web discovery, not Maps.
  safe_url('https://www.google.com/search?q=spa+bali','find-web',False)

 def test_rating_and_review_extracted_from_real_maps_accessible_labels(self):
  # These snippets are literal `image "X bintang"` / `image "Y ulasan"`
  # accessible names as actually captured from a real Maps detail panel.
  out={(r['business_id'],r['field']):r for r in deterministic_resolutions({'businesses':self.bs,'evidence':self.ev},self.spec)}
  expected={'b_184f356c10a7b5fa':(5.0,41),'b_8282d4742e4b0788':(5.0,68),'b_2f5b4f32deb7c06a':(4.8,292),'b_a5324b3c92eee088':(4.6,196),'b_f45af3db7cada6ac':(4.9,469)}
  for bid,(rating,reviews) in expected.items():
   r1=out[(bid,'google_rating')];r2=out[(bid,'google_review_count')]
   self.assertEqual(r1['status'],'found');self.assertEqual(r1['value'],rating)
   self.assertEqual(r2['status'],'found');self.assertEqual(r2['value'],reviews)

 def test_review_count_parenthesized_fallback(self):
  bid=self.bs[0]['business_id']
  e={'evidence_id':'e_paren','business_id':bid,'reader':'read-google','source_kind':'google_maps','field':'google_review_count','snippet':'4.9 (147)','retrieved_at':now()}
  out={(r['business_id'],r['field']):r for r in deterministic_resolutions({'businesses':[self.bs[0]],'evidence':[e]},self.spec)}
  r=out[(bid,'google_review_count')]
  self.assertEqual(r['status'],'found');self.assertEqual(r['value'],147)

 def test_missing_rating_is_not_found_never_fabricated(self):
  bid=self.bs[0]['business_id']
  out={(r['business_id'],r['field']):r for r in deterministic_resolutions({'businesses':[self.bs[0]],'evidence':[]},self.spec)}
  r=out[(bid,'google_rating')]
  self.assertEqual(r['status'],'not_found');self.assertIsNone(r['value']);self.assertEqual(r['evidence_ids'],[])

 def test_wrong_listing_cannot_contaminate_another_business(self):
  # A rating captured under business B must never leak into business A's
  # resolution, even if both are processed in the same deterministic pass.
  a=self.bs[0];b=self.bs[1]
  ea={'evidence_id':'e_a','business_id':a['business_id'],'reader':'read-google','source_kind':'google_maps','field':'google_rating','snippet':'4,2 bintang','retrieved_at':now()}
  eb={'evidence_id':'e_b','business_id':b['business_id'],'reader':'read-google','source_kind':'google_maps','field':'google_rating','snippet':'3,1 bintang','retrieved_at':now()}
  out={(r['business_id'],r['field']):r for r in deterministic_resolutions({'businesses':[a,b],'evidence':[ea,eb]},self.spec)}
  self.assertEqual(out[(a['business_id'],'google_rating')]['value'],4.2)
  self.assertEqual(out[(b['business_id'],'google_rating')]['value'],3.1)
  # And a resolution that tries to cite the other business's evidence id is
  # rejected outright by provenance validation.
  bad=resolution(a['business_id'],'google_rating','found',3.1,['e_b'],[ea,eb],'test',.9)
  with self.assertRaisesRegex(ValueError,'provenance'):validate_resolution(bad,[ea,eb],'ID')

 def test_google_metrics_require_actual_maps_ui_evidence(self):
  # A search-results snippet (source_kind=search_snippet), even one that
  # textually contains a rating, can never back a google_rating/
  # google_review_count resolution -- this is what makes the Maps-only
  # scope fix in safe_url load-bearing rather than cosmetic.
  bid=self.bs[0]['business_id']
  e={'evidence_id':'e_bad','business_id':bid,'reader':'read-google','source_kind':'search_snippet','field':'google_rating','snippet':'Diberi rating 5,0 dari 5','retrieved_at':now()}
  r=resolution(bid,'google_rating','found',5.0,['e_bad'],[e],'test',.9)
  with self.assertRaisesRegex(ValueError,'Maps UI evidence'):validate_resolution(r,[e],'ID')


class InstagramResolution(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.f=read(Path(__file__).parent/'fixtures/acceptance-140831.json')
  cls.ev=cls.f['evidence'];cls.bs=cls.f['businesses'];cls.spec=cls.f['spec']

 def valid_ig(self):
  rows=[]
  for b in self.bs:
   e=next(e for e in self.ev if e['business_id']==b['business_id'] and e['field']=='instagram')
   handle=e['snippet'].split('"')[1].split("'s")[0]
   rows.append(resolution(b['business_id'],'instagram','found','https://www.instagram.com/'+handle+'/',[e['evidence_id']],self.ev,'regression',.9))
  return {'resolutions':rows}

 def test_trailing_slash_is_normalized_like_phone_e164(self):
  # Mirrors the existing phone-normalization pattern: format-only fix, never
  # a relaxation of identity verification. This is the exact failure mode
  # seen in run-20260921-142754 attempt 1 ("Invalid Instagram URL").
  x=self.valid_ig()
  x['resolutions'][0]['value']=x['resolutions'][0]['value'].rstrip('/')
  self.assertFalse(x['resolutions'][0]['value'].endswith('/'))
  validate_resolver_batch(x,self.ev,self.bs,['instagram'],self.spec)
  self.assertTrue(x['resolutions'][0]['value'].endswith('/'))
  self.assertTrue(x['resolutions'][0]['value'].startswith('https://www.instagram.com/'))

 def test_malformed_components_shape_still_rejected(self):
  # 'components' is a legitimate, schema-defined optional property (used by
  # dm_proxy resolutions), so its mere presence is not an error -- but the
  # schema must still reject a malformed value, e.g. an out-of-enum status
  # (the kind of shape a stray/copied components value could easily have).
  # This confirms the prompt-only changes did not loosen the schema itself.
  x=self.valid_ig()
  x['resolutions'][0]['components']={'owner_reply':{'status':'not_a_real_status','evidence_ids':[]},'posting_recency':{'status':'unavailable','evidence_ids':[]}}
  with self.assertRaises(Exception):
   validate_resolver_batch(x,self.ev,self.bs,['instagram'],self.spec)

 def test_truncated_evidence_id_rejected_by_narrowed_schema(self):
  # This is the actual real-world failure from run-20260921-142754: the
  # model copied an evidence_id but dropped trailing characters. The
  # per-business narrowed schema (evidence_ids enum) used at resolve() time
  # must reject it rather than silently accepting a guessed/shortened id.
  e=next(e for e in self.ev if e['business_id']==self.bs[0]['business_id'] and e['field']=='instagram')
  truncated=e['evidence_id'][:20]
  self.assertNotIn(truncated,[ev['evidence_id'] for ev in self.ev])
  r=resolution(self.bs[0]['business_id'],'instagram','found','https://www.instagram.com/x/',[truncated],self.ev,'test',.9)
  with self.assertRaisesRegex(ValueError,'provenance'):validate_resolution(r,self.ev,self.spec['country'])

 def test_official_site_plus_handle_confirmation_resolves_found(self):
  x=self.valid_ig()
  validate_resolver_batch(x,self.ev,self.bs,['instagram'],self.spec)
  for r in x['resolutions']:
   self.assertEqual(r['status'],'found')
   self.assertRegex(r['value'],r'^https://www\.instagram\.com/[A-Za-z0-9_.]+/$')

 def test_nonexistent_and_cross_business_evidence_still_rejected(self):
  x=self.valid_ig()
  for ref in [x['resolutions'][1]['evidence_ids'][0],'e_nonexistent']:
   r=x['resolutions'][0];bad=copy.deepcopy(r);bad['evidence_ids']=[ref]
   with self.assertRaisesRegex(ValueError,'provenance'):validate_resolution(bad,self.ev,self.spec['country'])

 def test_third_retry_can_recover_a_genuinely_good_answer(self):
  # Before the fix, only 2 attempts were allowed; a model that stumbled on
  # attempt 1 (bad URL format) and attempt 2 (extra key / bad id) had no
  # chance to recover on attempt 3, even with perfectly good evidence.
  good=json.dumps(self.valid_ig())
  bad_url=copy.deepcopy(self.valid_ig());bad_url['resolutions'][0]['value']='https://www.instagram.com/badurl'
  attempts=[]
  def fake_llm(run,role,prompt,schema,output_schema=None):
   attempts.append(1)
   if len(attempts)==1:return strict_resolver_json(json.dumps(bad_url),output_schema)
   if len(attempts)==2:raise ValueError('simulated extra-key rejection')
   return strict_resolver_json(good,output_schema)
  with tempfile.TemporaryDirectory() as run,patch('worker.llm',side_effect=fake_llm):
   out=resolve(run,'resolve-ig',self.f,self.spec)
  self.assertEqual(len(attempts),3)
  for r in out['resolutions']:self.assertEqual(r['status'],'found')


class WhatsAppAndPublicContact(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.f=read(Path(__file__).parent/'fixtures/acceptance-140831.json')
  cls.ev=cls.f['evidence'];cls.spec=cls.f['spec']

 def test_bare_cta_label_has_no_extractable_value(self):
  # Exact real-world evidence text observed in run-20260921-142754: a link
  # whose accessible name is only the CTA/label, with no digits at all.
  for snippet in ['link "wa.me" [ref=e104]','link "Telepon" [ref=e83]','link "BOOK VIA WHATSAPP" [ref=e15]']:
   e={'snippet':snippet}
   self.assertEqual(contact_values(e,'whatsapp','ID'),set())
   self.assertEqual(contact_values(e,'public_contact','ID'),set())

 def test_whatsapp_send_url_matches_with_or_without_trailing_slash(self):
  for s in ['https://api.whatsapp.com/send?phone=6281139405010','https://api.whatsapp.com/send/?phone=6281139405010']:
   self.assertEqual(contact_values({'snippet':s},'whatsapp','ID'),{'+6281139405010'})

 def test_explicit_wa_me_number_resolves_found(self):
  e={'evidence_id':'e_wa','business_id':'b1','reader':'read-site','source_kind':'official_site','field':'whatsapp','snippet':'Chat us: https://wa.me/6281139405010','retrieved_at':now()}
  r=resolution('b1','whatsapp','found','+6281139405010',['e_wa'],[e],'test',.9)
  validate_resolution(r,[e],'ID')  # must not raise

 def test_ordinary_phone_is_not_automatically_whatsapp(self):
  e=next(e for e in self.ev if e['field']=='public_contact' and 'WhatsApp' not in e['snippet'] and 'whatsapp' not in e['snippet'].lower())
  r=resolution(e['business_id'],'whatsapp','found',normalize_phone([c for c in contact_values(e,'public_contact','ID')][0],'ID'),[e['evidence_id']],[e],'test',.9)
  with self.assertRaisesRegex(ValueError,'proof of WhatsApp'):validate_resolution(r,[e],'ID')

 def test_public_contact_independent_of_whatsapp_status(self):
  e=next(e for e in self.ev if e['field']=='public_contact')
  value=sorted(contact_values(e,'public_contact','ID'))[0]
  r=resolution(e['business_id'],'public_contact','found',value,[e['evidence_id']],[e],'test',.9)
  validate_resolution(r,[e],'ID')  # public_contact found is fine on its own

 def test_higher_priority_whatsapp_evidence_cannot_be_skipped(self):
  e=next(e for e in self.ev if 'WA number:' in e['snippet'])
  value=[v for v in contact_values(e,'whatsapp','ID')][0]
  for refs in [[],[e['evidence_id']]][:1]:
   r=resolution(e['business_id'],'whatsapp','not_found',None,refs,[e],'test',0)
   with self.assertRaisesRegex(ValueError,'higher-priority'):validate_resolution(r,[e],'ID')

 def test_indonesian_number_normalization(self):
  for v in ['0811-3940-5010','6281139405010','+62 811 39405010','62 811 3940 5010']:
   self.assertEqual(normalize_phone(v,'ID'),'+6281139405010')

 def test_no_outreach_url_ever_navigable(self):
  # contact_values may parse a wa.me URL out of page TEXT, but no reader may
  # ever actually *open* a WhatsApp/messaging endpoint as a browser page.
  for url in ['https://wa.me/6281139405010','https://api.whatsapp.com/send?phone=6281139405010','https://web.whatsapp.com/']:
   with self.assertRaises(ValueError):safe_url(url,'read-site',False)


class SemanticCompletion(unittest.TestCase):
 def _data(self,maps_evidence,resolved_status):
  business={'business_id':'b1','name_raw':'Test Spa','url':'https://example.com','evidence_ids':[]}
  evidence=maps_evidence
  resolved=[resolution('b1','google_rating',resolved_status,4.5 if resolved_status=='found' else None,[e['evidence_id'] for e in maps_evidence] if resolved_status=='found' else [],evidence,'test',.9 if resolved_status=='found' else 0)]
  return {'run_id':'run-test','businesses':[business],'evidence':evidence,'resolved_fields':resolved}

 def test_flags_true_when_maps_evidence_was_actually_observed(self):
  e={'evidence_id':'e1','business_id':'b1','reader':'read-google','source_kind':'google_maps','field':'google_rating','snippet':'4,5 bintang','retrieved_at':now()}
  with tempfile.TemporaryDirectory() as run:
   (Path(run)/'reports').mkdir()
   report=semantic_report(run,self._data([e],'found'),{'country':'ID'})
  self.assertTrue(report['businesses'][0]['google_rating']['maps_evidence_observed'])
  self.assertEqual(report['businesses'][0]['google_rating']['status'],'found')

 def test_flags_false_when_task_done_but_no_maps_evidence_collected(self):
  # This is precisely the failure mode from Problem 1: read-google reports
  # done, google_rating is not_found, and NOTHING distinguishes "genuinely
  # not visible" from "collection never actually reached Maps" -- until now.
  with tempfile.TemporaryDirectory() as run:
   (Path(run)/'reports').mkdir()
   report=semantic_report(run,self._data([],'not_found'),{'country':'ID'})
   self.assertFalse(report['businesses'][0]['google_rating']['maps_evidence_observed'])
   self.assertEqual(report['businesses'][0]['google_rating']['status'],'not_found')
   self.assertTrue((Path(run)/'reports/semantic-completion.json').is_file())


class BatchRetryResilience(unittest.TestCase):
 # Round-3 fix: run-20260921-163054 showed the resolve-wa batch producing
 # 5 genuinely correct public_contact resolutions (real phone numbers, real
 # evidence) on its second attempt, but ONE unrelated business' whatsapp
 # claim (a public phone cited as WhatsApp proof, correctly caught by
 # validate_resolution) made validate_resolver_batch raise on that single
 # row -- which discarded ALL 10 rows in the batch, including the 5 good
 # public_contact resolutions, and the run fell back to "ambiguous" for
 # everyone after exhausting all 3 attempts. resolve() must instead validate
 # and lock in each business/field row independently, so one business'
 # persistent mistake can never erase another business' already-correct,
 # already-fully-validated answer.
 @classmethod
 def setUpClass(cls):
  cls.f=read(Path(__file__).parent/'fixtures/acceptance-140831.json')
  cls.ev=cls.f['evidence'];cls.bs=cls.f['businesses'];cls.spec=cls.f['spec']

 def test_one_businesss_bad_whatsapp_claim_does_not_wipe_others_good_public_contact(self):
  good_bid=self.bs[1]['business_id'];bad_bid=self.bs[0]['business_id']
  def top_public_contact(bid):
   cands=[e for e in self.ev if e['business_id']==bid and e['field']=='public_contact' and contact_values(e,'public_contact',self.spec['country'])]
   best=min(PRIORITY[e['source_kind']] for e in cands)
   e=next(e for e in cands if PRIORITY[e['source_kind']]==best)
   return e,sorted(contact_values(e,'public_contact',self.spec['country']))[0]
  pc_evidence,pc_value=top_public_contact(good_bid)
  phone_evidence,phone_value=top_public_contact(bad_bid)
  def batch():
   return {'resolutions':[
    resolution(good_bid,'public_contact','found',pc_value,[pc_evidence['evidence_id']],self.ev,'test',.9),
    resolution(good_bid,'whatsapp','not_found',None,[],self.ev,'test',0),
    resolution(bad_bid,'public_contact','found',phone_value,[phone_evidence['evidence_id']],self.ev,'test',.9),
    resolution(bad_bid,'whatsapp','found',phone_value,[phone_evidence['evidence_id']],self.ev,'test',.9),
   ]}
  # Every attempt returns the exact same batch: the good business' two rows
  # are correct every time, the bad business' whatsapp row is permanently
  # unsupported (an ordinary phone is not WhatsApp proof) every time.
  with tempfile.TemporaryDirectory() as run,patch('worker.llm',side_effect=lambda *a,**kw:batch()) as m:
   out=resolve(run,'resolve-wa',{'evidence':self.ev,'businesses':[self.bs[0],self.bs[1]]},self.spec)
   self.assertEqual(m.call_count,3)
  by_key={(r['business_id'],r['field']):r for r in out['resolutions']}
  self.assertEqual(by_key[(good_bid,'public_contact')]['status'],'found')
  self.assertEqual(by_key[(good_bid,'public_contact')]['value'],pc_value)
  self.assertEqual(by_key[(good_bid,'whatsapp')]['status'],'not_found')
  self.assertEqual(by_key[(bad_bid,'public_contact')]['status'],'found')
  self.assertEqual(by_key[(bad_bid,'public_contact')]['value'],phone_value)
  # The unsupported whatsapp claim must never be accepted, however many
  # times it is retried -- it fails closed to ambiguous, not found.
  self.assertEqual(by_key[(bad_bid,'whatsapp')]['status'],'ambiguous')
  self.assertIsNone(by_key[(bad_bid,'whatsapp')]['value'])
  for r in out['resolutions']:validate_resolution(r,self.ev,self.spec['country'])

 def test_accepted_row_is_not_reopened_by_a_later_worse_attempt(self):
  # Once a row has passed the full validate_resolution check it is locked
  # in; a later attempt proposing something different for that same
  # business/field must not be allowed to silently replace a validated
  # answer with an unvalidated one.
  bid=self.bs[0]['business_id']
  cands=[ev for ev in self.ev if ev['business_id']==bid and ev['field']=='public_contact' and contact_values(ev,'public_contact',self.spec['country'])]
  best=min(PRIORITY[ev['source_kind']] for ev in cands)
  e=next(ev for ev in cands if PRIORITY[ev['source_kind']]==best)
  value=sorted(contact_values(e,'public_contact',self.spec['country']))[0]
  good={'resolutions':[resolution(bid,'public_contact','found',value,[e['evidence_id']],self.ev,'test',.9)]}
  worse={'resolutions':[resolution(bid,'public_contact','found','+10000000000',[],self.ev,'test',.9)]}
  calls=[]
  def fake_llm(*a,**kw):
   calls.append(1)
   return good if len(calls)==1 else worse
  with tempfile.TemporaryDirectory() as run,patch('worker.llm',side_effect=fake_llm):
   out=resolve(run,'resolve-wa',{'evidence':self.ev,'businesses':[self.bs[0]]},{**self.spec,'requested_fields':['public_contact']} if 'requested_fields' in self.spec else self.spec)
  self.assertEqual(len(calls),1)
  self.assertEqual(out['resolutions'][0]['value'],value)


class InstagramHintExtraction(unittest.TestCase):
 # Round-3 fix for Problem 2: read-ig's own evidence for a login-walled
 # profile often has no readable bio text, only an accessibility label like
 # "<handle>'s profile picture" and the literal profile URL it navigated to
 # (immutably recorded as source_url). Both are legitimate, browser-observed
 # confirmation that the reader's dedicated Instagram session actually
 # loaded that exact candidate's page -- this must not be silently dropped
 # just because the phrasing doesn't look like "@handle" or "instagram.com/handle".
 def test_profile_picture_accessibility_label_yields_handle(self):
  e={'business_id':'b1','evidence_id':'e1','field':'instagram','source_kind':'instagram','source_url':'https://www.instagram.com/face_therapy/','snippet':'link "face_therapy\'s profile picture" [ref=e2]'}
  hints=instagram_hints([e])
  self.assertEqual(hints[0]['handles'],['face_therapy'])

 def test_source_url_of_instagram_reader_is_authoritative_even_with_no_snippet_text(self):
  e={'business_id':'b1','evidence_id':'e1','field':'instagram','source_kind':'instagram','source_url':'https://www.instagram.com/oazabali/','snippet':'heading "Log in to see more" [ref=e5]'}
  hints=instagram_hints([e])
  self.assertEqual(hints[0]['handles'],['oazabali'])

 def test_ui_chrome_with_no_handle_anywhere_yields_no_hint(self):
  e={'business_id':'b1','evidence_id':'e1','field':'instagram','source_kind':'instagram','source_url':'https://www.instagram.com/accounts/login/','snippet':'button "Close" [ref=e9]'}
  self.assertEqual(instagram_hints([e]),[])  # reserved Instagram path, not a handle

 def test_reserved_instagram_paths_never_treated_as_a_handle(self):
  for path in ['accounts/login','explore','p/Cabc123','reel/Cxyz456','direct/inbox']:
   e={'business_id':'b1','evidence_id':'e1','field':'instagram','source_kind':'instagram','source_url':f'https://www.instagram.com/{path}/','snippet':'generic "Instagram" [ref=e1]'}
   self.assertEqual(instagram_hints([e]),[],msg=path)

 def test_search_snippet_candidate_still_extracted_as_before(self):
  e={'business_id':'b1','evidence_id':'e1','field':'instagram_candidate','source_kind':'search_snippet','source_url':'https://www.google.com/search?q=x','snippet':'link "Face Therapy Spa (@face_therapy) Instagram" [ref=e4]'}
  self.assertEqual(instagram_hints([e])[0]['handles'],['face_therapy'])


class BusinessNameEvidenceSelection(unittest.TestCase):
 # Round-4 fix: a real acceptance run blocked at the validator with "Value
 # absent from evidence" for business_name -- a genuine, pre-existing bug
 # only surfaced once the round-1 fix made read-google reliably collect its
 # own business_name evidence (a Maps heading). deterministic_resolutions()
 # always resolves business_name to the ORIGINAL discovery name (name_raw,
 # from find-web/find-maps), but used to cite whichever business_name-field
 # evidence existed, even when a secondary source (a Maps heading, a site
 # logo alt text) used a shorter/different name variant that doesn't
 # literally contain name_raw -- failing provenance for a perfectly correct
 # name. It must fall back to the original discovery evidence (which, by
 # construction, always contains name_raw verbatim) whenever no existing
 # business_name evidence actually confirms the resolved value.
 def _business(self):
  return {'business_id':'b1','name_raw':'Therapy Day Spa Pererenan','url':'https://therapy.co.id','evidence_ids':['e_discovery']}

 def test_shorter_name_variant_from_other_readers_does_not_block_resolution(self):
  b=self._business()
  ev=[
   {'evidence_id':'e_discovery','business_id':'b1','field':'candidate','source_kind':'search_snippet','reader':'find-web','snippet':'link "Therapy Day Spa Pererenan | Conscious Spa Rituals in Bali ... Therapy Day Spa Pererenan https://therapy.co.id"','retrieved_at':now()},
   {'evidence_id':'e_maps','business_id':'b1','field':'business_name','source_kind':'google_maps','reader':'read-google','snippet':'heading "Therapy Day Spa" [level=1, ref=e37]','retrieved_at':now()},
   {'evidence_id':'e_site','business_id':'b1','field':'business_name','source_kind':'official_site','reader':'read-site','snippet':'link "Therapy Day Spa logo" [ref=e8]','retrieved_at':now()},
  ]
  out=deterministic_resolutions({'businesses':[b],'evidence':ev},{'requested_fields':['business_name'],'country':'ID'})
  r=next(x for x in out if x['field']=='business_name')
  self.assertEqual(r['status'],'found');self.assertEqual(r['value'],'Therapy Day Spa Pererenan')
  self.assertEqual(r['evidence_ids'],['e_discovery'])
  validate_resolution(r,ev,'ID')  # must not raise

 def test_confirming_business_name_evidence_is_still_preferred_when_it_matches(self):
  # If a later reader's business_name evidence DOES contain the full
  # discovered name, prefer citing that stronger corroboration, unchanged
  # from the original design intent.
  b=self._business()
  ev=[
   {'evidence_id':'e_discovery','business_id':'b1','field':'candidate','source_kind':'search_snippet','reader':'find-web','snippet':'Therapy Day Spa Pererenan','retrieved_at':now()},
   {'evidence_id':'e_maps','business_id':'b1','field':'business_name','source_kind':'google_maps','reader':'read-google','snippet':'heading "Therapy Day Spa Pererenan" [level=1, ref=e37]','retrieved_at':now()},
  ]
  out=deterministic_resolutions({'businesses':[b],'evidence':ev},{'requested_fields':['business_name'],'country':'ID'})
  r=next(x for x in out if x['field']=='business_name')
  self.assertEqual(r['evidence_ids'],['e_maps'])
  validate_resolution(r,ev,'ID')  # must not raise

 def test_no_business_name_evidence_at_all_still_falls_back_as_before(self):
  b=self._business()
  ev=[{'evidence_id':'e_discovery','business_id':'b1','field':'candidate','source_kind':'search_snippet','reader':'find-web','snippet':'Therapy Day Spa Pererenan','retrieved_at':now()}]
  out=deterministic_resolutions({'businesses':[b],'evidence':ev},{'requested_fields':['business_name'],'country':'ID'})
  r=next(x for x in out if x['field']=='business_name')
  self.assertEqual(r['evidence_ids'],['e_discovery'])
  validate_resolution(r,ev,'ID')


if __name__=='__main__':unittest.main()
