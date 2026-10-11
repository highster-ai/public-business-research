# STEP 5 regression tests — read-site DNS/bad-URL per-URL isolation.
# Authorized narrowly-scoped repair for run-20260922-200250 (socket.gaierror
# escaped the site_reader per-URL boundary and crashed 25-business batches).
# The browser is faked; no live research and no network calls are made here.
import sys, json, socket, unittest, uuid, urllib.parse, tempfile, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import quality as Q
from core import write, digest, read as core_read, verify_evidence

RUN_TIME = '2026-09-22T00:00:00+00:00'
GOOD_PAGE = 'heading "Test Spa" [level=1]\nPhone: +62 812 3456789\nlink "Contact" [ref=e5]'
SPEC = {'requested_fields': ['public_contact', 'whatsapp', 'instagram'], 'country': 'ID', 'area': 'Canggu, Bali'}
GAI = socket.gaierror(8, 'nodename nor servname provided, or not known')

def biz(bid, name='Test Spa'):
    return {'business_id': bid, 'name_raw': name}

def site_ev(bid, url):
    return {'evidence_id': 'e_' + bid, 'business_id': bid, 'field': 'website', 'source_kind': 'google_maps',
            'snippet': 'Observed link target: link "Website: x " [ref=e8] -> ' + url,
            'source_url': 'https://www.google.com/maps/place/x', 'retrieved_at': RUN_TIME, 'reader': 'read-google',
            'snapshot_id': 's_x', 'snapshot_sha256': 'x'}

class FakeBrowser:
    # Mimics the real Browser contract used by site_reader: page() calls
    # open({'url': ...}) and reads the JSON snapshot string; target() calls
    # link({'ref': ...}).
    def __init__(self, run, dead_hosts=(), fail=None):
        self.run = run; self.dead = set(dead_hosts); self.opens = []; self.fail = fail
    def store(self, url, observation):
        sid = 's_' + uuid.uuid4().hex
        write(Q.Path(self.run) / 'raw/read-site/snapshots' / (sid + '.json'),
              {'snapshot_id': sid, 'url': url, 'retrieved_at': RUN_TIME, 'reader': 'read-site', 'observation': observation})
        return json.dumps({'snapshot_id': sid, 'url': url, 'untrusted_page_text': observation}, ensure_ascii=False)
    def open(self, args, **kwargs):
        url = args['url']; self.opens.append(url)
        if self.fail is not None:
            raise self.fail
        if urllib.parse.urlsplit(url).hostname in self.dead:
            raise GAI
        return self.store(url, GOOD_PAGE)
    def link(self, args, **kwargs):
        raise GAI

class SiteReaderCase(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp()).resolve()

    def reader(self, businesses, evidence, dead_hosts=(), fail=None):
        browser = FakeBrowser(self.run, dead_hosts, fail)
        out = Q.site_reader(self.run, browser, businesses, evidence, SPEC)
        coverage = core_read(self.run / 'raw/read-site/semantic-coverage.json')['businesses']
        return browser, out, {c['business_id']: c for c in coverage}

    def test_1_gaierror_isolated_per_url(self):
        # A single unresolvable URL must not raise out of site_reader.
        _, out, cov = self.reader([biz('b_dead')], [site_ev('b_dead', 'https://deadhost-lgfix.invalid/')], dead_hosts=('deadhost-lgfix.invalid',))
        self.assertEqual(cov['b_dead']['source_failures'][0]['url'], 'https://deadhost-lgfix.invalid/')
        self.assertIn('nodename nor servname', cov['b_dead']['source_failures'][0]['reason'])

    def test_2_later_businesses_still_processed(self):
        # The business after the dead one must still be researched.
        _, out, cov = self.reader([biz('b_dead'), biz('b_ok')],
                                  [site_ev('b_dead', 'https://deadhost-lgfix.invalid/'), site_ev('b_ok', 'https://goodspa.example/')],
                                  dead_hosts=('deadhost-lgfix.invalid',))
        self.assertTrue(cov['b_ok']['extracted'])
        self.assertFalse(cov['b_dead']['extracted'])

    def test_3_honest_missing_not_fabricated(self):
        # A dead URL yields a recorded failure, no synthesized evidence, and
        # honest missing fields.
        _, out, cov = self.reader([biz('b_dead')], [site_ev('b_dead', 'https://deadhost-lgfix.invalid/')], dead_hosts=('deadhost-lgfix.invalid',))
        self.assertEqual([e for e in out['evidence'] if e['business_id'] == 'b_dead'], [])
        self.assertEqual(cov['b_dead']['extracted'], [])
        self.assertEqual(cov['b_dead']['missing'], ['public_contact', 'whatsapp', 'instagram_candidate'])
        self.assertIn('deadhost-lgfix.invalid', json.dumps(cov['b_dead']['source_failures']))

    def test_4_provenance_intact_for_unaffected_business(self):
        # Evidence for the unaffected business keeps full provenance and
        # passes the production evidence verifier.
        _, out, cov = self.reader([biz('b_dead'), biz('b_ok')],
                                  [site_ev('b_dead', 'https://deadhost-lgfix.invalid/'), site_ev('b_ok', 'https://goodspa.example/')],
                                  dead_hosts=('deadhost-lgfix.invalid',))
        rows = [e for e in out['evidence'] if e['business_id'] == 'b_ok']
        self.assertTrue(rows)
        row = rows[0]
        self.assertEqual(row['reader'], 'read-site')
        self.assertEqual(row['source_kind'], 'official_site')
        snap = core_read(self.run / 'raw/read-site/snapshots' / (row['snapshot_id'] + '.json'))
        self.assertEqual(row['snapshot_sha256'], digest(snap['observation']))
        self.assertEqual(row['source_url'], snap['url'])
        verify_evidence(self.run, row)  # raises on any provenance defect

    def test_5_valid_url_normal_path(self):
        # A healthy URL follows the unchanged normal path and yields evidence.
        browser, out, cov = self.reader([biz('b_ok')], [site_ev('b_ok', 'https://goodspa.example/')])
        self.assertIn('https://goodspa.example/', browser.opens)
        fields = {e['field'] for e in out['evidence']}
        self.assertIn('public_contact', fields)
        self.assertIn('public_contact', cov['b_ok']['extracted'])

    def test_6_malformed_url_rejected_before_browser_and_dns(self):
        # Snippet punctuation must be rejected deterministically before any
        # browser/DNS access and recorded as an honest failure.
        browser, out, cov = self.reader([biz('b_bad')], [site_ev('b_bad', 'https://foo.com).')])
        self.assertEqual(browser.opens, [])
        self.assertEqual(cov['b_bad']['source_failures'][0]['reason'], 'Invalid URL syntax; rejected before browser/DNS access')

    def test_7_unexpected_programming_exception_not_swallowed(self):
        # Only the documented per-URL failure classes are isolated; an
        # unrelated programming error must still surface.
        with self.assertRaises(KeyError):
            self.reader([biz('b_ok')], [site_ev('b_ok', 'https://goodspa.example/')], fail=KeyError('boom'))

    def test_8_multiple_dead_urls_isolated(self):
        # Several dead URLs fail independently; the batch completes.
        browser, out, cov = self.reader([biz('b_d1'), biz('b_d2'), biz('b_ok')],
                                        [site_ev('b_d1', 'https://dead1-lgfix.invalid/'), site_ev('b_d2', 'https://dead2-lgfix.invalid/'), site_ev('b_ok', 'https://goodspa.example/')],
                                        dead_hosts=('dead1-lgfix.invalid', 'dead2-lgfix.invalid'))
        self.assertTrue(cov['b_d1']['source_failures'] and cov['b_d2']['source_failures'])
        self.assertIn('public_contact', cov['b_ok']['extracted'])

    def test_9_batch_completes_despite_dns_failure(self):
        # The reader returns its normal output shape and writes coverage.
        _, out, cov = self.reader([biz('b_dead'), biz('b_ok')],
                                  [site_ev('b_dead', 'https://deadhost-lgfix.invalid/'), site_ev('b_ok', 'https://goodspa.example/')],
                                  dead_hosts=('deadhost-lgfix.invalid',))
        self.assertEqual(out['businesses'], [])
        self.assertTrue(all(e['business_id'] == 'b_ok' for e in out['evidence']))
        self.assertEqual(len(cov), 2)

    def test_10_no_cross_business_contamination(self):
        # A dead business's failure must not leak evidence or coverage into
        # its neighbour.
        browser, out, cov = self.reader([biz('b_dead'), biz('b_ok')],
                                        [site_ev('b_dead', 'https://deadhost-lgfix.invalid/'), site_ev('b_ok', 'https://goodspa.example/')],
                                        dead_hosts=('deadhost-lgfix.invalid',))
        self.assertEqual({e['business_id'] for e in out['evidence']}, {'b_ok'})
        self.assertEqual(cov['b_dead']['pages'], 1)
        self.assertTrue(cov['b_dead']['extracted'] == [] and cov['b_ok']['extracted'])

    def test_11_link_target_gaierror_isolated(self):
        # The same isolation applies at the link-target boundary: a gaierror
        # while resolving a link target is audited, never crashes the batch,
        # and page-level extraction continues.
        Q.site_reader(self.run, LinkFailBrowser(self.run), [biz('b_ok')], [site_ev('b_ok', 'https://goodspa.example/')], SPEC)
        coverage = core_read(self.run / 'raw/read-site/semantic-coverage.json')['businesses']
        self.assertEqual(coverage[0]['business_id'], 'b_ok')
        self.assertIn('public_contact', coverage[0]['extracted'])
        audit = [json.loads(l) for l in (self.run / 'audit/read-site.jsonl').read_text().splitlines()]
        self.assertIn('link_target_unavailable', [a['event'] for a in audit])

    def test_12_url_syntax_gate_matrix(self):
        # Deterministic validation, no network: rejects structurally
        # impossible URLs; keeps legitimate public ones, including
        # international hostnames.
        bad = ['https://foo.com).', 'https://foo bar.com/', 'https://foo..com/', 'ftp://foo.com/', 'https:///path', 'https://foo.com:99999/', '']
        good = ['https://goodspa.example/', 'http://korraspa.com/', 'https://миспа.сайт/', 'https://foo.com./']
        for u in bad:
            self.assertFalse(Q.valid_site_url(u), u)
        for u in good:
            self.assertTrue(Q.valid_site_url(u), u)

class LinkFailBrowser(FakeBrowser):
    def open(self, args, **kwargs):
        url = args['url']; self.opens.append(url)
        return self.store(url, GOOD_PAGE)
    def link(self, args, **kwargs):
        raise GAI

if __name__ == '__main__':
    unittest.main()
