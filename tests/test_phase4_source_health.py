import json, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))

from core import check, read, resolution, write
from pipeline import acceptance_coverage, deterministic_resolutions, write_run_report
from quality import google_reader, resolve_contacts_and_instagram
from source_health import load_source_health, merge_source_health
import source_health


class RestrictedBrowser:
 def __init__(self,run):self.run=Path(run)
 def open(self,args):
  sid='s_'+'a'*32
  url='https://www.google.com/maps/place/Test+Spa/'
  observation='Google Maps\nYou are seeing a limited view. Sign in to continue.'
  write(self.run/'raw/read-google/snapshots'/f'{sid}.json',{
   'snapshot_id':sid,'url':url,'retrieved_at':'2026-10-10T00:00:00+00:00',
   'reader':'read-google','observation':observation})
  return json.dumps({'snapshot_id':sid,'url':url,'untrusted_page_text':observation})


class SourceAccessHonestyTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.run=Path(self.temp.name)/'run-test'
  (self.run/'raw/read-google/snapshots').mkdir(parents=True)
  (self.run/'reports').mkdir(parents=True)
  self.business={'business_id':'b1','name_raw':'Test Spa','url':'https://www.google.com/maps/place/Test+Spa/','evidence_ids':[]}
  self.spec={'area':'Dubai Marina','country':'AE','requested_fields':['google_rating','google_review_count','public_contact']}
 def tearDown(self):self.temp.cleanup()

 def test_restricted_snapshot_is_failed_not_not_public_and_source_degraded(self):
  result=google_reader(self.run,RestrictedBrowser(self.run),[self.business],self.spec)
  evidence=result['evidence']
  google=deterministic_resolutions({'businesses':[self.business],'evidence':evidence},self.spec)
  contact=resolve_contacts_and_instagram([self.business],evidence,['public_contact'],'AE')['resolutions']
  rows=google+contact
  by_field={r['field']:r for r in rows}
  for field in self.spec['requested_fields']:
   self.assertEqual(by_field[field]['status'],'access_restricted')
   self.assertNotEqual(by_field[field]['status'],'not_public')
   check('resolution',by_field[field])
  coverage,overall=acceptance_coverage({'businesses':[self.business],'resolved_fields':rows},self.spec)
  self.assertEqual(overall,0.0)
  self.assertEqual(coverage['google_rating']['resolved_percent'],0.0)
  self.assertEqual(coverage['google_rating']['failed_percent'],100.0)
  self.assertEqual(coverage['google_rating']['access_restricted_percent'],100.0)
  health=merge_source_health(self.run)
  self.assertEqual(health['google_maps']['status'],'degraded')
  self.assertEqual(health['google_maps']['pages_restricted'],1)
  self.assertFalse(health['google_maps']['blocked'])

  approval={'overall_useful_cell_percent':overall,'field_coverage':coverage,
   'gate_failures':['google_rating','google_review_count','public_contact'],
   'warnings':[],'iteration_exhausted_tasks':[],'canonical_sha256':'test','execution':{}}
  write_run_report(self.run,'partial_success',{'businesses':[self.business]},approval)
  report=(self.run/'reports/run-report.md').read_text()
  self.assertIn('Access restricted: 100.0%',report)
  self.assertIn('status: degraded; pages ok: 0; restricted: 1; blocked: false',report)

 def test_consecutive_restrictions_block_source_for_remaining_pages(self):
  old_limit,old_backoff=source_health.SOURCE_RESTRICTION_LIMIT,source_health.SOURCE_RESTRICTION_BACKOFF_SECONDS
  try:
   source_health.SOURCE_RESTRICTION_LIMIT=2;source_health.SOURCE_RESTRICTION_BACKOFF_SECONDS=0
   source_health.record_source_page(self.run,'google_maps',True,'read-google')
   self.assertEqual(source_health.before_source_page(self.run,'google_maps','read-google')['status'],'degraded')
   state=source_health.record_source_page(self.run,'google_maps',True,'read-google')
   self.assertEqual(state['status'],'blocked');self.assertTrue(state['blocked'])
   with self.assertRaisesRegex(RuntimeError,'Source blocked'):
    source_health.before_source_page(self.run,'google_maps','read-google')
  finally:
   source_health.SOURCE_RESTRICTION_LIMIT=old_limit;source_health.SOURCE_RESTRICTION_BACKOFF_SECONDS=old_backoff

 def test_blocked_source_is_access_restricted_not_not_found(self):
  # Regression: once the source blocks mid-batch, the remaining businesses are
  # never opened. Their Maps fields must still be honestly access_restricted
  # (source restriction prevented verification), never not_found/not_public,
  # and the reader must still complete rather than fail the task.
  old_limit,old_backoff=source_health.SOURCE_RESTRICTION_LIMIT,source_health.SOURCE_RESTRICTION_BACKOFF_SECONDS
  try:
   source_health.SOURCE_RESTRICTION_LIMIT=1;source_health.SOURCE_RESTRICTION_BACKOFF_SECONDS=0
   second={'business_id':'b2','name_raw':'Second Spa','url':'https://www.google.com/maps/place/Second+Spa/','evidence_ids':[]}
   result=google_reader(self.run,RestrictedBrowser(self.run),[self.business,second],self.spec)
   self.assertTrue(merge_source_health(self.run)['google_maps']['blocked'])
   evidence=result['evidence']
   google=deterministic_resolutions({'businesses':[self.business,second],'evidence':evidence},self.spec)
   contact=resolve_contacts_and_instagram([self.business,second],evidence,['public_contact'],'AE')['resolutions']
   by={(r['business_id'],r['field']):r for r in google+contact}
   for bid in ['b1','b2']:
    for field in self.spec['requested_fields']:
     self.assertEqual(by[(bid,field)]['status'],'access_restricted')
     self.assertNotIn(by[(bid,field)]['status'],{'not_found','not_public'})
     check('resolution',by[(bid,field)])
   coverage,overall=acceptance_coverage({'businesses':[self.business,second],'resolved_fields':list(by.values())},self.spec)
   self.assertEqual(overall,0.0)
   self.assertEqual(coverage['google_rating']['resolved_percent'],0.0)
   self.assertEqual(coverage['google_rating']['access_restricted_percent'],100.0)
  finally:
   source_health.SOURCE_RESTRICTION_LIMIT=old_limit;source_health.SOURCE_RESTRICTION_BACKOFF_SECONDS=old_backoff

 def test_reader_records_source_health_within_the_fail_closed_guard(self):
  # Regression: the reader runs inside Guard (app/guard.py), which denies any
  # write outside the role's own raw/<role>/ staging. Recording source health
  # must therefore stage role-locally and never raise, or the whole task fails
  # the moment Google restricts a page.
  from guard import Guard
  profile=Path(self.temp.name)/'profile';profile.mkdir(parents=True,exist_ok=True)
  with Guard(self.run,'read-google',profile,browser=True):
   result=google_reader(self.run,RestrictedBrowser(self.run),[self.business],self.spec)
  self.assertTrue(result['evidence'])
  self.assertEqual((self.run/'raw/read-google/source-health.json').exists(),True)
  self.assertFalse((self.run/'reports/source-health.json').exists())
  health=merge_source_health(self.run)
  self.assertEqual(health['google_maps']['status'],'degraded')
  self.assertEqual(load_source_health(self.run)['google_maps']['status'],'degraded')


if __name__=='__main__':unittest.main()
