# STEP 8 regression tests — read-google Maps-metric semantic completion.
# Authorized narrowly-scoped repair for run-20260922-232944: verified Maps
# listing b_800153446857da41 (Blackpink spa Bali) had NO rating/review-count
# element in its successfully-inspected overview, but maps_semantic_success
# required both metrics from every verified listing, triaging the batch and
# blocking 19 downstream tasks. MISSING DATA != FAILED EXTRACTION.
# The browser is faked; no live research and no network calls are made here
# (test_15 loads the preserved run's snapshot read-only).
import sys, json, unittest, unittest.mock, uuid, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import quality as Q
from core import write, read as core_read, verify_evidence, now
from pipeline import deterministic_resolutions

AREA = 'Canggu, Pererenan, Berawa, Seminyak, Bali'
SPEC = {'area': AREA, 'country': 'ID'}
PRESERVED_57DA41 = ROOT / 'runs/run-20260922-232944/raw/read-google/snapshots/s_d69a4b1f400c40e18bd88cb7229f5a51.json'
WEB_TARGETS = {'e5': ('Website: testspa.example', 'https://testspa.example/')}

def body(name, rating=None, count=None, phone=True, website=True):
    b = 'heading "%s" [level=1, ref=e1]\n- button "Massage spa" [ref=e2]\n' % name
    b += '- region "Information for %s" [ref=e3]\n' % name
    if rating is not None: b += '- image "%s stars "\n' % rating
    if count is not None: b += '- image "%s reviews"\n' % count
    b += '- button "Address: Jl. Pantai Berawa, Bali " [ref=e4]\n'
    if phone: b += '- button "Phone: +62 812 3456789 " [ref=e5]\n'
    if website: b += '- link "Website: testspa.example " [ref=e5]\n'
    return b

def biz(bid, name):
    return {'business_id': bid, 'name_raw': name,
            'url': 'https://www.google.com/maps/place/' + name.replace(' ', '+') + '/'}

class FakeBrowser:
    # Mimics the real Browser contract used by maps_listing/google_reader:
    # page() calls open({'url': ...}) and reads the JSON snapshot string;
    # target() calls link({'ref': ...}) and reads the trailing target line.
    # link() never navigates: the snapshot URL stays the listing page, exactly
    # like the real Browser.link.
    def __init__(self, run, bodies, targets=None, fail=None):
        self.run = run; self.bodies = bodies; self.opens = []
        self.targets = targets or WEB_TARGETS; self.fail = fail; self.url = None
    def store(self, url, observation):
        sid = 's_' + uuid.uuid4().hex
        write(Q.Path(self.run) / 'raw/read-google/snapshots' / (sid + '.json'),
              {'snapshot_id': sid, 'url': url, 'retrieved_at': now(), 'reader': 'read-google', 'observation': observation})
        return json.dumps({'snapshot_id': sid, 'url': url, 'untrusted_page_text': observation}, ensure_ascii=False)
    def open(self, args, **kwargs):
        url = args['url']; self.opens.append(url)
        if self.fail is not None: raise self.fail
        self.url = url
        return self.store(url, self.bodies[url])
    def link(self, args, **kwargs):
        label, target = self.targets[args['ref']]
        return self.store(self.url, self.bodies[self.url] + '\nObserved link target: link "%s" -> %s' % (label, target))

class ReadGoogleCase(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp()).resolve()

    def reader(self, businesses, browser=None):
        browser = browser or FakeBrowser(self.run, {b['url']: body(b['name_raw'], '4.8', '96') for b in businesses})
        ev = Q.google_reader(self.run, browser, businesses, SPEC)['evidence']
        return ev, core_read(self.run / 'raw/read-google/semantic-coverage.json')

    # 1. verified listing + rating + review count -> success
    def test_1_verified_listing_with_metrics_succeeds(self):
        _, rep = self.reader([biz('b1', 'Test Spa')])
        self.assertTrue(rep['semantic_success'])
        self.assertEqual(rep['businesses'][0]['extracted'], ['google_rating', 'google_review_count', 'public_contact', 'website'])
        self.assertEqual(rep['businesses'][0]['missing'], [])

    # 2. verified listing + both metrics missing after successful inspection
    #    -> reader success with honest missing fields
    def test_2_verified_listing_missing_both_metrics_succeeds(self):
        browser = FakeBrowser(self.run, {biz('b2', 'Quiet Spa')['url']: body('Quiet Spa')})
        _, rep = self.reader([biz('b2', 'Quiet Spa')], browser)
        self.assertTrue(rep['semantic_success'])
        row = rep['businesses'][0]
        self.assertEqual(row['missing'], ['google_rating', 'google_review_count'])
        self.assertEqual(row['extracted'], ['public_contact', 'website'])
        self.assertTrue(row['read_ok'])
        self.assertEqual(row['reason'], 'Not visible in verified listing overview')

    # 3. rating found + review count missing -> success
    def test_3_rating_found_count_missing_succeeds(self):
        browser = FakeBrowser(self.run, {biz('b3', 'Test Spa')['url']: body('Test Spa', rating='4.8')})
        _, rep = self.reader([biz('b3', 'Test Spa')], browser)
        self.assertTrue(rep['semantic_success'])
        row = rep['businesses'][0]
        self.assertEqual(row['missing'], ['google_review_count'])
        self.assertIn('google_rating', row['extracted'])
        self.assertNotIn('google_review_count', row['extracted'])

    # 4. rating missing + review count found -> success
    def test_4_count_found_rating_missing_succeeds(self):
        browser = FakeBrowser(self.run, {biz('b4', 'Test Spa')['url']: body('Test Spa', count='96')})
        _, rep = self.reader([biz('b4', 'Test Spa')], browser)
        self.assertTrue(rep['semantic_success'])
        row = rep['businesses'][0]
        self.assertEqual(row['missing'], ['google_rating'])
        self.assertIn('google_review_count', row['extracted'])
        self.assertNotIn('google_rating', row['extracted'])

    # 5. website missing does not fail batch (tolerated as today)
    def test_5_website_missing_does_not_fail_batch(self):
        browser = FakeBrowser(self.run, {biz('b5', 'Test Spa')['url']: body('Test Spa', '4.8', '96', website=False)}, targets={})
        _, rep = self.reader([biz('b5', 'Test Spa')], browser)
        self.assertTrue(rep['semantic_success'])
        self.assertEqual(rep['businesses'][0]['missing'], ['website'])

    # 6. phone missing does not fail batch
    def test_6_phone_missing_does_not_fail_batch(self):
        browser = FakeBrowser(self.run, {biz('b6', 'Test Spa')['url']: body('Test Spa', '4.8', '96', phone=False)})
        _, rep = self.reader([biz('b6', 'Test Spa')], browser)
        self.assertTrue(rep['semantic_success'])
        self.assertEqual(rep['businesses'][0]['missing'], ['public_contact'])

    # 7. identity-unverifiable listing preserves existing conservative behavior
    def test_7_identity_unverifiable_tolerated(self):
        # A listing whose heading is a different business keeps the identity
        # refusal path and is tolerated, never treated as missing metrics.
        browser = FakeBrowser(self.run, {biz('b7', 'Test Spa')['url']: body('Other Spa Branch')}, targets={})
        _, rep = self.reader([biz('b7', 'Test Spa')], browser)
        self.assertTrue(rep['semantic_success'])
        row = rep['businesses'][0]
        self.assertTrue(row['identity_unverifiable'])
        self.assertFalse(row['read_ok'])
        self.assertIn('not uniquely identified', row['reason'])

    # 8. browser/read failure remains semantic failure
    def test_8_read_failure_stays_semantic_failure(self):
        browser = FakeBrowser(self.run, {}, fail=RuntimeError('net::ERR_ABORTED'))
        with self.assertRaisesRegex(ValueError, 'Semantic extraction incomplete'):
            self.reader([biz('b8', 'Test Spa')], browser)
        rep = core_read(self.run / 'raw/read-google/semantic-coverage.json')
        self.assertFalse(rep['semantic_success'])
        self.assertFalse(rep['businesses'][0]['read_ok'])

    # 9. malformed/incomplete extraction state is not silently converted to
    #    not_found: a provenance failure mid-extraction keeps read_ok False
    def test_9_extraction_provenance_failure_not_silently_missing(self):
        browser = FakeBrowser(self.run, {biz('b9', 'Test Spa')['url']: body('Test Spa', '4.8', '96')})
        businesses = [biz('b9', 'Test Spa')]
        real_evidence = Q.evidence
        def broken(run, role, bid, field, snapshot, snippet, kind):
            if field == 'google_rating': raise RuntimeError('Evidence does not match immutable browser snapshot')
            return real_evidence(run, role, bid, field, snapshot, snippet, kind)
        with unittest.mock.patch('quality.evidence', side_effect=broken):
            with self.assertRaisesRegex(ValueError, 'Semantic extraction incomplete'):
                self.reader(businesses, browser)
        rep = core_read(self.run / 'raw/read-google/semantic-coverage.json')
        self.assertFalse(rep['semantic_success'])
        self.assertFalse(rep['businesses'][0]['read_ok'])

    # 10. one business with missing metrics does not block later businesses
    def test_10_missing_metrics_do_not_block_later_businesses(self):
        entries = [biz('b_a', 'Test Spa'), biz('b_b', 'Quiet Spa'), biz('b_c', 'Later Spa')]
        browser = FakeBrowser(self.run, {entries[0]['url']: body('Test Spa', '4.8', '96'),
                                         entries[1]['url']: body('Quiet Spa'),
                                         entries[2]['url']: body('Later Spa', '5.0', '13')})
        _, rep = self.reader(entries, browser)
        self.assertTrue(rep['semantic_success'])
        byid = {r['business_id']: r for r in rep['businesses']}
        self.assertEqual(byid['b_a']['missing'], [])
        self.assertEqual(byid['b_b']['missing'], ['google_rating', 'google_review_count'])
        self.assertEqual(byid['b_c']['missing'], [])
        self.assertTrue(all(r['read_ok'] for r in byid.values()))

    # 11. one business with a real read failure follows retry policy without
    #     contaminating other businesses
    def test_11_read_failure_isolated_to_its_business(self):
        entries = [biz('b_ok', 'Test Spa'), biz('b_bad', 'Broken Spa')]
        browser = FakeBrowser(self.run, {entries[0]['url']: body('Test Spa', '4.8', '96'),
                                         entries[1]['url']: body('Broken Spa', '4.2', '7')})
        orig_open = browser.open
        def flaky_open(args, **kwargs):
            if 'Broken' in args['url']: raise RuntimeError('net::ERR_ABORTED')
            return orig_open(args, **kwargs)
        browser.open = flaky_open
        with self.assertRaisesRegex(ValueError, 'Semantic extraction incomplete'):
            self.reader(entries, browser)  # bounded retry/triage signal
        rep = core_read(self.run / 'raw/read-google/semantic-coverage.json')
        byid = {r['business_id']: r for r in rep['businesses']}
        self.assertFalse(byid['b_bad']['read_ok'])
        self.assertTrue(byid['b_ok']['read_ok'])
        self.assertEqual(byid['b_ok']['missing'], [])

    # 12. no cross-business provenance contamination
    def test_12_no_cross_business_contamination(self):
        entries = [biz('b_a', 'Test Spa'), biz('b_b', 'Quiet Spa')]
        browser = FakeBrowser(self.run, {entries[0]['url']: body('Test Spa', '4.8', '96'),
                                         entries[1]['url']: body('Quiet Spa')})
        ev, rep = self.reader(entries, browser)
        ids = {b['business_id'] for b in entries}
        self.assertTrue(ev)
        self.assertTrue({e['business_id'] for e in ev}.issubset(ids))
        for e in ev:
            verify_evidence(self.run, e)
        self.assertTrue(rep['semantic_success'])

    # 13. found metric provenance passes verify_evidence (observed Maps
    #     format fixtures: "N.N stars " and "N,NPP reviews" with separators)
    def test_13_found_metric_provenance_passes_verify(self):
        businesses = [biz('b13', 'Test Spa')]
        browser = FakeBrowser(self.run, {businesses[0]['url']: body('Test Spa', '4.8', '1,198')})
        ev = self.reader(businesses, browser)[0]
        for e in ev:
            verify_evidence(self.run, e)  # raises on any provenance defect
        self.assertIn('google_rating', [e['field'] for e in ev])
        self.assertIn('google_review_count', [e['field'] for e in ev])
        rows = deterministic_resolutions({'businesses': businesses, 'evidence': ev},
                                         {'requested_fields': ['google_rating', 'google_review_count']})
        by_field = {r['field']: r for r in rows}
        self.assertEqual(by_field['google_rating']['value'], 4.8)
        self.assertEqual(by_field['google_review_count']['value'], 1198)

    # 14. missing metric never creates fabricated numeric evidence
    def test_14_missing_metric_never_fabricated(self):
        businesses = [biz('b14', 'Quiet Spa')]
        browser = FakeBrowser(self.run, {businesses[0]['url']: body('Quiet Spa')})
        ev = self.reader(businesses, browser)[0]
        self.assertEqual([e for e in ev if e['field'].startswith('google_')], [])
        rows = deterministic_resolutions({'businesses': businesses, 'evidence': ev},
                                         {'requested_fields': ['business_name', 'google_rating', 'google_review_count']})
        by_field = {r['field']: r for r in rows}
        self.assertEqual(by_field['google_rating']['status'], 'not_found')
        self.assertEqual(by_field['google_rating']['value'], None)
        self.assertEqual(by_field['google_rating']['evidence_ids'], [])
        self.assertEqual(by_field['google_review_count']['status'], 'not_found')
        self.assertEqual(by_field['google_review_count']['value'], None)
        self.assertEqual(by_field['business_name']['status'], 'found')

    # 15. the exact 57da41 preserved evidence reproduces the old failure and
    #     passes under the corrected semantics
    def test_15_preserved_57da41_evidence_old_fails_new_passes(self):
        if not PRESERVED_57DA41.exists():
            self.skipTest('private preserved run is not part of the release tree')
        snap = core_read(PRESERVED_57DA41)
        businesses = [{'business_id': 'b_800153446857da41', 'name_raw': 'Blackpink spa Bali',
                       'url': 'https://www.google.com/maps/place/Blackpink+spa+Bali/'}]
        browser = FakeBrowser(self.run, {businesses[0]['url']: snap['observation']},
                              targets={'e62': ('Website: blackpink-spa-bali.com', 'https://blackpink-spa-bali.com/')})
        ev, rep = self.reader(businesses, browser)
        row = rep['businesses'][0]
        self.assertTrue(rep['semantic_success'])
        self.assertTrue(row['read_ok'])
        self.assertEqual(row['access_restricted'], ['google_rating', 'google_review_count'])
        self.assertEqual(row['extracted'], ['public_contact', 'website'])
        self.assertEqual(row['reason'], 'Access restricted by Google Maps UI')
        # Under the OLD policy (verified listing must yield both metrics) this
        # exact row was a semantic failure — prove the policy change is what
        # turned the preserved failure into an honest success.
        old_ok = all(f in row['extracted'] for f in ['google_rating', 'google_review_count']) or row.get('identity_unverifiable')
        self.assertFalse(old_ok)
        # Downstream: honest access_restricted, no fabricated numeric values.
        rows = deterministic_resolutions({'businesses': businesses, 'evidence': ev},
                                         {'requested_fields': ['business_name', 'google_rating', 'google_review_count']})
        by_field = {r['field']: r for r in rows}
        self.assertEqual(by_field['google_rating']['status'], 'access_restricted')
        self.assertEqual(by_field['google_review_count']['status'], 'access_restricted')
        self.assertEqual(by_field['business_name']['status'], 'found')

if __name__ == '__main__':
    unittest.main()
