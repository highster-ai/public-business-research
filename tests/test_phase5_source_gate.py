"""Phase 5.1 -- host-level source gate regression tests.

Release blocker: once Google restricted three Maps pages, the run's
`read-reviews` task (which also opens Google Maps) kept spawning workers ~20
minutes later, rediscovering the same login wall. The host must therefore gate
dispatch on source health: when a source is blocked for the run, no further
task that opens that source may spawn, and the batches that would have run are
recorded access_restricted -- citing the snapshot that established the block --
never not_found/not_public, and never claimed resolved.
"""
import json, os, sys, tempfile, unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))

import pipeline
from core import read, write
from quality import resolve_contacts_and_instagram
from source_health import merge_source_health

SID='s_'+'b'*32
LINE='You are seeing a limited view. Sign in to continue.'
URL='https://www.google.com/maps/place/Test+Spa/'
SPEC={'category':'spa wellness massage','area':'Dubai Marina','country':'AE','count':5,
 'sources':['google_maps','web','instagram'],
 'requested_fields':['business_name','instagram','whatsapp','public_contact','google_rating','google_review_count','dm_proxy','dm_measured','owner'],
 'dm_probe':False,'formats':['md','xlsx','pdf']}
LOCAL={'backend':'local','image':None,'image_digest':None,'limits':{'sandbox':'macos'}}


def build_run(tmp,sizes=None):
 '''Build a real (local-backend) kanban board in a temp ROOT. The patches must
 stay active while run_loop executes, so the caller owns the ExitStack --
 run_loop's own preflight/ROOT must see the temp tree, not the checkout's.'''
 stack=ExitStack()
 stack.enter_context(patch('pipeline.ROOT',tmp))
 stack.enter_context(patch.object(pipeline,'preflight',lambda s,browser_check=True,**kw:{'passed':True}))
 stack.enter_context(patch('execution.runtime_details',return_value=dict(LOCAL)))
 if sizes:stack.enter_context(patch.dict(pipeline.BATCH_SIZES,sizes))
 (tmp/'runs').mkdir(exist_ok=True)
 run=pipeline.make_run(SPEC,selected_backend='local')
 return stack,run,json.loads((run/'control/graph.json').read_text())


def seed(tmp,run,graph,done,blocked_snapshot=True):
 '''Seed the immutable dedupe output, the block snapshot + role-local source
 health, and mark every task done except the gated ones under test.'''
 businesses=[{'business_id':'b%d'%i,'name_raw':'Spa %d'%i,'url':URL,'evidence_ids':[]} for i in range(SPEC['count'])]
 write(run/'raw/dedupe/dedupe.json',{'businesses':businesses})
 if blocked_snapshot:
  write(run/'raw/read-google/snapshots'/(SID+'.json'),{'snapshot_id':SID,'url':URL,
   'retrieved_at':'2026-10-11T01:20:00+00:00','reader':'read-google','observation':'Google Maps\n'+LINE})
  write(run/'raw/read-google/source-health.json',{'google_maps':{'status':'blocked','pages_ok':0,
   'pages_restricted':3,'blocked':True,'consecutive_restrictions':3,'restriction_limit':3,
   'blocked_at':'2026-10-11T01:20:00+00:00','updated_at':'2026-10-11T01:20:00+00:00',
   'last_restriction':{'snapshot_id':SID,'line':LINE}}})
  merge_source_health(run) # run_loop publishes the merged view before the gate reads it
 conn=pipeline.configure(run)
 for key,node in graph.items():
  if key in done:conn.execute("UPDATE tasks SET status='done' WHERE id=?",(node['id'],))
 conn.commit()
 return conn,businesses


class SourceGateIntegration(unittest.TestCase):
 '''A block established mid-run must stop every remaining google_maps task
 from spawning and record the skipped batches access_restricted.'''

 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.tmp=Path(self.temp.name)
  self.env=patch.dict(os.environ,{'LEADGEN_EXECUTION_BACKEND':'local'});self.env.start();self.addCleanup(self.env.stop)

 def test_blocked_source_spawns_nothing_and_skipped_batches_are_access_restricted(self):
  # Two read-google batches (batch-01 established the block; batch-02/03 never
  # ran) plus the single read-reviews batch.
  stack,run,graph=build_run(self.tmp,{'read-google':2});self.addCleanup(stack.close)
  gated_done={'read-google-batch-01'}
  done={k for k in graph if k not in {'read-google-batch-02','read-google-batch-03','read-reviews-batch-01'}}|gated_done
  conn,businesses=seed(self.tmp,run,graph,done)
  calls=[]
  with patch.object(pipeline,'launch_worker',side_effect=lambda *a,**k:calls.append(a[1]) or self.fail('a google_maps task was spawned while the source was blocked')):
   pipeline.run_loop(str(run),'local')
  self.assertEqual(calls,[],'no worker may spawn once the source is blocked')

  # Every gated batch the host refused to spawn is done, not left claimed.
  by_key={r['title']:r['status'] for r in conn.execute('SELECT title,status FROM tasks')}
  for key in ['read-google-batch-02','read-google-batch-03','read-reviews-batch-01']:
   self.assertEqual(by_key[key],'done',key)

  # The skipped read-google batches record access_restricted for the fields
  # they own, citing the snapshot that established the block.
  evidence=[]
  for key,slices in [('read-google-batch-02',(2,4)),('read-google-batch-03',(4,5))]:
   out=read(graph[key]['output'])
   self.assertEqual(out['businesses'],[])
   self.assertEqual(out['skipped_source_blocked']['source'],'google_maps')
   self.assertEqual(out['skipped_source_blocked']['block_snapshot_id'],SID)
   self.assertEqual(out['skipped_source_blocked']['fields_access_restricted'],
    ['google_rating','google_review_count','public_contact','website'])
   for b in businesses[slices[0]:slices[1]]:
    fields={e['field'] for e in out['evidence'] if e['business_id']==b['business_id']}
    self.assertEqual(fields,{'google_rating','google_review_count','public_contact','website'})
   evidence+=out['evidence']

  # read-reviews owns only the evidence-only proxy field owner_reply, which has
  # no resolution column and cannot be cited from its own staging: it is
  # skipped with an honest empty result rather than an invented restriction.
  reviews=read(graph['read-reviews-batch-01']['output'])
  self.assertEqual(reviews['evidence'],[])
  self.assertEqual(reviews['skipped_source_blocked']['fields_access_restricted'],[])

  # The skipped batches resolve as access_restricted -- never found/not_found.
  skipped=[b for i,b in enumerate(businesses) if i>=2]
  spec=dict(SPEC,requested_fields=['google_rating','google_review_count','public_contact'])
  google=pipeline.deterministic_resolutions({'businesses':skipped,'evidence':evidence},spec)
  contact=resolve_contacts_and_instagram(skipped,evidence,['public_contact'],'AE')['resolutions']
  by={(r['business_id'],r['field']):r['status'] for r in google+contact}
  for b in skipped:
   for field in ['google_rating','google_review_count','public_contact']:
    self.assertEqual(by[(b['business_id'],field)],'access_restricted')
    self.assertNotIn(by[(b['business_id'],field)],{'found','not_found','not_public'})

  # The skip is logged in status and the run report, with the block cited.
  status=read(run/'reports/status.json')
  logged={s['task'] for s in status['skipped_source_blocked']}
  self.assertEqual(logged,{'read-google-batch-02','read-google-batch-03','read-reviews-batch-01'})
  persisted=read(run/'reports/skipped-source-blocked.json')
  self.assertEqual(len(persisted),3)
  self.assertTrue(all(s['source']=='google_maps' and s['block_snapshot_id']==SID for s in persisted))
  # ...and the run report carries the same section (the supervisor reads the
  # persisted file, so an empty file here would silently drop it).
  pipeline.write_run_report(run,'partial_success',{'businesses':businesses},
   {'overall_useful_cell_percent':0.0,'field_coverage':{},'gate_failures':[],'warnings':[],
    'iteration_exhausted_tasks':[],'canonical_sha256':'x','execution':{}})
  report=(run/'reports/run-report.md').read_text()
  self.assertIn('## Skipped (source blocked)',report)
  self.assertIn('read-review',report)

 def test_gate_never_clobbers_an_earlier_skip_record(self):
  # Regression: the gate runs before every dispatch, so a later pass with
  # nothing left to skip previously overwrote the persisted record with [].
  # The status/report then lost every skip. The gate must add records, never
  # erase them.
  stack,run,graph=build_run(self.tmp,{'read-google':2});self.addCleanup(stack.close)
  pending={'read-google-batch-03','read-reviews-batch-01'}
  conn,businesses=seed(self.tmp,run,graph,{k for k in graph if k not in pending})
  write(run/'reports/skipped-source-blocked.json',[{'task':'read-google-batch-02','role':'read-google',
   'source':'google_maps','block_snapshot_id':SID,'fields_access_restricted':['google_rating']}])
  first=pipeline.skip_blocked_source_tasks(run,conn,graph)
  self.assertEqual([s['task'] for s in first],['read-google-batch-03','read-reviews-batch-01'])
  # A second pass finds nothing to skip and must leave the host-owned file alone.
  self.assertEqual(pipeline.skip_blocked_source_tasks(run,conn,graph),[])
  persisted=read(run/'reports/skipped-source-blocked.json')
  self.assertEqual([s['task'] for s in persisted],['read-google-batch-02'])

 def test_healthy_source_is_never_gated(self):
  # Control: with no block recorded, the gate must not touch a single task --
  # the same board would otherwise dispatch normally.
  stack,run,graph=build_run(self.tmp,{'read-google':2});self.addCleanup(stack.close)
  conn,businesses=seed(self.tmp,run,graph,set(),blocked_snapshot=False)
  skipped=pipeline.skip_blocked_source_tasks(run,conn,graph)
  self.assertEqual(skipped,[])
  statuses={r['title']:r['status'] for r in conn.execute('SELECT title,status FROM tasks')}
  self.assertNotIn('done',{statuses['read-google-batch-02'],statuses['read-reviews-batch-01']})


class RunReportSkipSection(unittest.TestCase):
 def test_report_names_the_skipped_batches(self):
  tmp=Path(tempfile.mkdtemp());self.addCleanup(lambda:__import__('shutil').rmtree(tmp,True))
  (tmp/'reports').mkdir(parents=True)
  write(tmp/'reports/skipped-source-blocked.json',[{'task':'read-google-batch-02','role':'read-google',
   'source':'google_maps','block_snapshot_id':SID,'fields_access_restricted':['google_rating','public_contact']}])
  approval={'overall_useful_cell_percent':0.0,'field_coverage':{},'gate_failures':[],'warnings':[],
   'iteration_exhausted_tasks':[],'canonical_sha256':'x','execution':{}}
  pipeline.write_run_report(tmp,'partial_success',{'businesses':[{'business_id':'b1'}]},approval)
  report=(tmp/'reports/run-report.md').read_text()
  self.assertIn('## Skipped (source blocked)',report)
  self.assertIn('read-google-batch-02',report)
  self.assertIn('skipped: source blocked',report)


if __name__=='__main__':unittest.main()
