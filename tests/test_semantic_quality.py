"""Semantic rules, with explicit synthetic positive proofs and real-run negatives."""
import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from core import *
from quality import *
from worker import resolve
from pipeline import deterministic_resolutions,semantic_report

class SemanticQuality(unittest.TestCase):
 def test_exact_live_reader_response_keeps_candidates_separate(self):
  from worker import parse_reader_json
  p=Path(__file__).parent/'fixtures/reader-012311.json'
  parsed=parse_reader_json(read(p)['response']);check('reader_output',parsed)
  self.assertEqual(parsed['observations'],[]);self.assertEqual(len(parsed['candidates']),5)
 def test_reader_parser_rejects_malformed_and_duplicate_keys(self):
  from worker import parse_reader_json
  for raw in ['{"observations":[],"candidates":[{"snippet":"broken}]}','{"observations":[],"observations":[]}']:
   with self.assertRaises(ValueError):parse_reader_json(raw)
 def test_reader_format_retry_still_checks_snapshot_provenance(self):
  from worker import reader
  spec={'sources':['web'],'country':'ID','count':1,'category':'spa','area':'Bali'}
  invalid=jsonschema.ValidationError('Wrong discovery section')
  candidate={'name_raw':'Test','url':'https://example.com/','snapshot_id':'s_'+'0'*32,'snippet':'Test'}
  with tempfile.TemporaryDirectory() as run,patch('worker.llm',side_effect=[invalid,{'candidates':[candidate],'observations':[]}]) as model:
   result=reader(run,'find-web',{},spec)
   self.assertEqual(model.call_count,2);self.assertEqual(result['businesses'],[])
 def test_reader_format_retry_is_bounded(self):
  from worker import reader
  spec={'sources':['web'],'country':'ID','count':1,'category':'spa','area':'Bali'}
  with tempfile.TemporaryDirectory() as run,patch('worker.llm',side_effect=jsonschema.ValidationError('Invalid')) as model:
   with self.assertRaises(jsonschema.ValidationError):reader(run,'find-web',{},spec)
   self.assertEqual(model.call_count,2)
 def test_maps_heading_before_url_settles_requires_refresh(self):
  from unittest.mock import Mock
  body='heading "Body Wonders Spa & Massage" [level=1]\nAddress: Pererenan'
  initial={'url':'https://www.google.com/maps/search/Body','untrusted_page_text':body}
  settled={**initial,'url':'https://www.google.com/maps/place/Body/'}
  browser=Mock();browser.capture.return_value=json.dumps(settled)
  with patch('quality.page',return_value=initial),patch('quality.time.sleep'):
   result=maps_listing(browser,{'name_raw':'Body Wonders Spa & Massage'},{'area':'Pererenan'})
  self.assertEqual(result['url'],settled['url']);browser.capture.assert_called_once()
 def e(self,id,field,snippet,kind='google_maps',bid='b1',url=None):
  return {'evidence_id':id,'business_id':bid,'field':field,'snippet':snippet,'source_kind':kind,'reader':'read-google' if kind=='google_maps' else 'read-site','source_url':url or ('https://www.google.com/maps/place/Test/' if kind=='google_maps' else 'https://test.example/'),'retrieved_at':now()}
 def run_resolver(self,ev,fields,bs=None):
  return resolve('unused','resolve-ig' if fields==['instagram'] else 'resolve-wa',{'businesses':bs or [{'business_id':'b1'}],'evidence':ev},{'requested_fields':fields,'country':'ID'})['resolutions']
 def test_profile_picture_and_url_not_identity(self):
  ev=[self.e('ig','instagram',"test_spa's profile picture",'instagram',url='https://www.instagram.com/test_spa/'),self.e('candidate','instagram_candidate','Test Spa Bali (@test_spa)','search_snippet')]
  self.assertEqual(self.run_resolver(ev,['instagram'])[0]['status'],'ambiguous')
 def test_maps_linked_official_site_verifies_profile(self):
  ev=[self.e('maps','website','Website -> https://test.example/'),self.e('site','instagram_candidate','Instagram -> https://www.instagram.com/test_spa/','official_site')]
  r=self.run_resolver(ev,['instagram'])[0];self.assertEqual(r['status'],'found');self.assertEqual(set(r['evidence_ids']),{'maps','site'});validate_instagram_identity(r,ev)
 def test_maps_direct_instagram_reference(self):
  ev=[self.e('maps','website','Website -> https://www.instagram.com/test_spa/')]
  self.assertEqual(self.run_resolver(ev,['instagram'])[0]['status'],'found')
 def test_unlinked_website_cannot_verify_profile(self):
  ev=[self.e('maps','website','Website -> https://different.example/'),self.e('site','instagram_candidate','Instagram -> https://www.instagram.com/test_spa/','official_site')]
  self.assertEqual(self.run_resolver(ev,['instagram'])[0]['status'],'ambiguous')
 def test_cross_business_official_link_is_not_identity(self):
  ev=[self.e('maps','website','Website -> https://test.example/',bid='b2'),self.e('site','instagram_candidate','Instagram -> https://www.instagram.com/test_spa/','official_site')]
  self.assertEqual(self.run_resolver(ev,['instagram'])[0]['status'],'ambiguous')
 def test_identity_validator_rejects_unsupported_found(self):
  r=resolution('b1','instagram','found','https://www.instagram.com/test_spa/',['e'],[],'test',1)
  with self.assertRaisesRegex(ValueError,'identity'):validate_instagram_identity(r,[])
 def test_visible_redirect_lineage_preserved(self):
  ev=[self.e('maps','website','Website -> https://test.example/'),self.e('redirect','website','Observed browser navigation: https://test.example/ -> https://linktr.ee/test','official_site',url='https://linktr.ee/test'),self.e('site','instagram_candidate','Instagram -> https://instagram.com/test_spa/','official_site',url='https://linktr.ee/test')]
  r=self.run_resolver(ev,['instagram'])[0];self.assertEqual(r['status'],'found');self.assertEqual(set(r['evidence_ids']),{'maps','redirect','site'})
 def test_different_linktree_profile_not_same_site(self):
  self.assertFalse(same_site('https://linktr.ee/test','https://linktr.ee/other'))
 def test_conflicting_official_profiles_stay_ambiguous(self):
  ev=[self.e('maps','website','Website -> https://test.example/'),self.e('a','instagram_candidate','https://instagram.com/first/','official_site'),self.e('b','instagram_candidate','https://instagram.com/second/','official_site')]
  self.assertEqual(self.run_resolver(ev,['instagram'])[0]['status'],'ambiguous')
 def test_contact_fields_independent_and_no_model_transcription(self):
  ev=[self.e('phone','public_contact','Phone: 0812-3899-7421')]
  with patch('worker.llm',side_effect=AssertionError('No transcription needed')):
   wa,phone=self.run_resolver(ev,['whatsapp','public_contact'])
  self.assertEqual(wa['status'],'not_found');self.assertEqual(phone['value'],'+6281238997421');self.assertEqual(phone['evidence_ids'],['phone'])
 def test_actual_target_not_cta(self):
  cta=self.e('cta','whatsapp','BOOK VIA WHATSAPP','official_site')
  self.assertEqual(self.run_resolver([cta],['whatsapp'])[0]['status'],'not_found')
  target=self.e('link','whatsapp','Observed link target: BOOK VIA WHATSAPP -> https://wa.me/6281238997421?text=Hello','official_site')
  r=self.run_resolver([cta,target],['whatsapp'])[0];self.assertEqual(r['value'],'+6281238997421');self.assertEqual(r['evidence_ids'],['link'])
 def test_one_ambiguous_business_does_not_discard_another(self):
  ev=[self.e('a','public_contact','0812-3899-7421'),self.e('b','public_contact','0811-3940-5010'),self.e('c','public_contact','0812-3899-7421',bid='b2')]
  rows=self.run_resolver(ev,['public_contact'],[{'business_id':'b1'},{'business_id':'b2'}]);self.assertEqual(rows[0]['status'],'ambiguous');self.assertEqual(rows[1]['status'],'found');self.assertEqual(rows[1]['evidence_ids'],['c'])
 def test_related_listing_excluded_from_primary_metrics(self):
  s={'untrusted_page_text':'heading "Test Spa" [level=1]\n image "4.8 stars"\n image "123 reviews"\n heading "People also search for" [level=2]\n image "1.0 stars 999 reviews"'}
  lines=primary_maps_lines(s);self.assertIn('123 reviews',' '.join(lines));self.assertNotIn('999',' '.join(lines))
 def test_verified_listing_with_only_name_is_honest_missing(self):
  # Updated for the run-20260922-232944 semantic repair: a verified listing
  # that was successfully inspected may legitimately lack rating/review-count
  # (MISSING DATA != FAILED EXTRACTION); only actual read failures are
  # semantic failures (see tests/test_readgoogle_semantic.py).
  with tempfile.TemporaryDirectory() as run:
   sid='s_'+'a'*32;url='https://www.google.com/maps/place/Test+Spa/';body='heading "Test Spa" [level=1]\nAddress: Bali'
   write(Path(run)/'raw/read-google/snapshots'/f'{sid}.json',{'snapshot_id':sid,'url':url,'reader':'read-google','retrieved_at':now(),'observation':body})
   class Fake:
    def open(self,args):return json.dumps({'snapshot_id':sid,'url':url,'untrusted_page_text':body})
   result=google_reader(run,Fake(),[{'business_id':'b1','name_raw':'Test Spa','url':url}],{'area':'Bali'})
   self.assertEqual(result['businesses'],[])
   row=read(Path(run)/'raw/read-google/semantic-coverage.json')['businesses'][0]
   self.assertTrue(row['read_ok']);self.assertEqual(row['missing'],['google_rating','google_review_count','public_contact','website'])
 def test_search_metric_baseline_not_accepted_as_maps(self):
  ev=[self.e('search','google_rating','5.0 stars','search_snippet')]
  rows=deterministic_resolutions({'businesses':[{'business_id':'b1'}],'evidence':ev},{'requested_fields':['google_rating']})
  self.assertEqual(rows[0]['status'],'not_found')
 def test_public_contact_does_not_count_as_wa_semantic_coverage(self):
  d={'run_id':'test','businesses':[{'business_id':'b1','name_raw':'Test'}],'evidence':[self.e('p','public_contact','0812-3899-7421')],'resolved_fields':[]}
  with tempfile.TemporaryDirectory() as run:r=semantic_report(run,d,{'country':'ID','requested_fields':['whatsapp','public_contact']})
  self.assertFalse(r['businesses'][0]['whatsapp']['numeric_contact_evidence_observed']);self.assertTrue(r['businesses'][0]['public_contact']['numeric_contact_evidence_observed'])
 def test_observed_http_website_is_only_tried_as_https(self):
  class Fake:
   def open(self,args):
    self.url=args['url'];return '{}'
  b=Fake();page(b,'http://www.pressurebali.com/');self.assertEqual(b.url,'https://www.pressurebali.com/')
  with self.assertRaises(ValueError):safe_url('http://www.pressurebali.com/','read-site',False)
 def test_other_branch_numbers_are_not_collected_for_pererenan(self):
  body='- link "Canggu +62 819 1677 4478" [ref=e1]\n- link "Pererenan +62 812 3919 8827" [ref=e2]'
  class Fake:
   def open(self,args):return json.dumps({'snapshot_id':'test','url':'https://test.example/','untrusted_page_text':body})
   def link(self,args):
    if args['ref']!='e2':raise AssertionError('Other branch must not be assigned to this business')
    return json.dumps({'snapshot_id':'test','url':'https://test.example/','untrusted_page_text':body+'\nObserved link target: Pererenan -> https://wa.me/6281239198827'})
  def observed(run,role,bid,field,snap,snippet,kind):return self.e('test-'+field,field,snippet,kind,bid)
  with tempfile.TemporaryDirectory() as run,patch('quality.evidence',side_effect=observed):
   out=site_reader(run,Fake(),[{'business_id':'b1','name_raw':'Esthetic Pererenan'}],[self.e('map','website','https://test.example/')],{'country':'ID'})
  self.assertTrue(out['evidence']);self.assertTrue(all('Canggu' not in e['snippet'] for e in out['evidence']))
  self.assertTrue(any(e['field']=='whatsapp' for e in out['evidence']))
 def test_worker_timeout_escalates_without_orphaning(self):
  from unittest.mock import Mock
  from pipeline import stop_worker
  import subprocess,signal
  p=Mock();p.pid=12345;p.poll.return_value=None;p.wait.side_effect=[subprocess.TimeoutExpired('worker',5),0]
  with patch('pipeline.os.killpg') as kill:stop_worker(p)
  self.assertEqual([c.args for c in kill.call_args_list],[(12345,signal.SIGTERM),(12345,signal.SIGKILL)])

if __name__=='__main__':unittest.main()
