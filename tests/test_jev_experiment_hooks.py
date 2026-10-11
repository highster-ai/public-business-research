# Regression gates for the Jev LIVE A/B experiment hooks (M1-M4).
# Contract: with every JEV_AB_* flag unset, the production route is
# byte-identical to the pre-experiment route; with flags on, Jev can only
# screen/suggest, never browse, never outreach, never mint canonical values,
# and every failure mode falls back to the unmodified Hermes path.
import inspect
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

import jev_experiment as je  # noqa: E402

KEY = 'test-ab-key-' + 'x' * 30
BID = 'b_' + '1' * 16
BID2 = 'b_' + '2' * 16

AB_FLAGS = ['JEV_AB_ENABLED', 'JEV_AB_OWNER', 'JEV_AB_MAPS', 'JEV_AB_OWNER_T',
            'JEV_AB_MAPS_T', 'JEV_AB_DIR', 'JEV_SHADOW_ENABLED', 'TYPESAFE_API_KEY']


def env(**kw):
    return mock.patch.dict(os.environ, {k: v for k, v in kw.items()}, clear=False)


def owner_evidence_row(bid, text, eid):
    return {'evidence_id': eid, 'business_id': bid,
            'source_url': 'https://www.google.com/maps/place/x',
            'source_kind': 'google_maps', 'snippet': text, 'raw_text': text,
            'retrieved_at': '2026-09-24T00:00:00+00:00', 'reader': 'find-owner',
            'snapshot_id': 's_' + 'a' * 32, 'snapshot_sha256': 'x', 'field': 'owner'}


def biz(bid=BID):
    return {'business_id': bid, 'name_raw': 'Test Spa Bali',
            'url': 'https://www.google.com/maps/place/x'}


class JevABHooks(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in AB_FLAGS}
        for k in AB_FLAGS:
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp(prefix='jevab-')

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def records(self):
        p = Path(self.tmp) / 'records.jsonl'
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    # ---- 1+2. flags default false; flag-off = production route ----

    def test_all_flags_default_false(self):
        self.assertFalse(je.enabled())
        self.assertFalse(je.owner_screening_enabled())
        self.assertFalse(je.maps_triage_enabled())

    def test_owner_screen_flag_off_is_noop(self):
        called = []
        with mock.patch.object(je, '_error'):
            out = je.owner_screen(self.tmp, {'businesses': [biz()], 'evidence': []},
                                  {'country': 'ID'}, lambda *a: called.append(a))
        self.assertIsNone(out)
        self.assertEqual(called, [])

    def test_maps_rescue_flag_off_is_noop(self):
        class FakeBrowser:
            pass
        with mock.patch.object(je, '_record'), mock.patch.object(je, '_error'):
            out = je.maps_rescue('/tmp/run-x', FakeBrowser(), biz(),
                                 {'area': 'Canggu, Bali'},
                                 ValueError('Exact Maps business listing not uniquely identified'))
        self.assertIsNone(out)

    def test_maps_verify_flag_off_writes_nothing(self):
        os.environ['JEV_AB_DIR'] = self.tmp
        je.maps_verify(self.tmp, {'untrusted_page_text': 'heading "x" [level=1]', 'url': 'u'},
                       biz(), {'area': 'a'})
        self.assertEqual(self.records(), [])

    def test_flag_off_sandbox_has_no_typesafe(self):
        import sandbox
        for role in ['resolve-owner', 'resolve-dm', 'find-maps', 'query-plan']:
            pol = sandbox.policy(self.tmp, role)
            self.assertNotIn('api.typesafe.ai', pol)
        self.assertIsNone(os.environ.get('JEV_SHADOW_ENABLED'))

    def test_flag_on_grants_typesafe_only_to_experimental_roles(self):
        import sandbox
        os.environ['JEV_AB_ENABLED'] = 'true'
        Path(self.tmp, 'control').mkdir()
        for role in ['resolve-owner', 'find-maps']:
            pol = sandbox.policy(self.tmp, role)
            self.assertIn('(allow network-outbound (remote ip "api.typesafe.ai:443"))', pol)
        for role in ['resolve-dm', 'query-plan', 'validator']:
            self.assertNotIn('api.typesafe.ai', sandbox.policy(self.tmp, role))

    # ---- 3+4. missing key / API error fail closed to Hermes ----

    def test_missing_key_falls_back_and_records_error(self):
        os.environ['JEV_AB_ENABLED'] = 'true'
        os.environ['JEV_AB_DIR'] = self.tmp  # TYPESAFE_API_KEY deliberately absent
        inp = {'businesses': [biz()], 'evidence': [owner_evidence_row(BID, 'Owned by Ani', 'e_1')]}
        def fail_hermes(*a):
            raise AssertionError('Hermes path must not be replaced by screening')
        out = je.owner_screen(self.tmp, inp, {'country': 'ID'}, fail_hermes)
        self.assertIsNone(out)  # wholesale Hermes fallback
        self.assertIn('MissingAPIKey', (Path(self.tmp) / 'errors.jsonl').read_text())

    def test_api_error_falls_back_per_business(self):
        os.environ['JEV_AB_ENABLED'] = 'true'
        os.environ['JEV_AB_DIR'] = self.tmp
        os.environ['TYPESAFE_API_KEY'] = KEY
        import jev_shadow
        with mock.patch.object(jev_shadow, '_post', side_effect=OSError('boom')):
            inp = {'businesses': [biz()], 'evidence': [owner_evidence_row(BID, 'Owned by Ani', 'e_1')]}
            out = je.owner_screen(self.tmp, inp, {'country': 'ID'},
                                  lambda *a: {'resolutions': [{'hermes': True}]})
        self.assertIsNone(out)  # nothing screened; Hermes batch intact

    # ---- 5+6. Jev cannot browse; Jev cannot outreach ----

    def test_jev_module_never_browses(self):
        src = (ROOT / 'app' / 'jev_experiment.py').read_text()
        self.assertNotIn('import browser', src)
        self.assertNotIn('from browser', src)
        self.assertNotIn('Browser(', src)
        self.assertNotIn('urllib.request', src)
        self.assertNotIn('subprocess', src)
        self.assertNotIn('os.system', src)

    def test_jev_never_outreaches_dm_probe_always_false(self):
        os.environ['JEV_AB_ENABLED'] = 'true'
        os.environ['JEV_AB_DIR'] = self.tmp
        os.environ['TYPESAFE_API_KEY'] = KEY
        import jev_shadow
        seen = {}
        def fake_post(state, question, key):
            seen['state'] = state
            return {'value': 0.05, 'input_tokens': 1, 'output_tokens': 1}
        with mock.patch.object(jev_shadow, '_post', side_effect=fake_post):
            inp = {'businesses': [biz()], 'evidence': [owner_evidence_row(BID, 'Owned by Ani', 'e_1')]}
            def fail_hermes(*a):
                raise AssertionError('screened business must skip Hermes')
            je.owner_screen(self.tmp, inp, {'country': 'ID'}, fail_hermes)
        self.assertIs(seen['state']['dm_probe'], False)
        for r in self.records():
            self.assertIs(r['dm_probe'], False)

    # ---- 7. Jev cannot mint canonical values ----

    def test_screened_out_is_not_found_null_value_real_validation(self):
        from core import validate_resolution
        os.environ['JEV_AB_ENABLED'] = 'true'
        os.environ['JEV_AB_DIR'] = self.tmp
        os.environ['TYPESAFE_API_KEY'] = KEY
        import jev_shadow
        with mock.patch.object(jev_shadow, '_post',
                               return_value={'value': 0.05, 'input_tokens': 1, 'output_tokens': 1}):
            inp = {'businesses': [biz()], 'evidence': [owner_evidence_row(BID, 'Spa in Canggu', 'e_1')]}
            out = je.owner_screen(self.tmp, inp, {'country': 'ID'},
                                  lambda *a: pytest_fail())
        r = out['resolutions'][0]
        self.assertEqual(r['status'], 'not_found')
        self.assertIsNone(r['value'])
        self.assertEqual(r['evidence_ids'], ['e_1'])
        validate_resolution(r, inp['evidence'], 'ID')  # full production validator
        self.assertIn('Jev live A/B screening', r['method'])

    def test_no_evidence_goes_to_hermes_not_screened(self):
        os.environ['JEV_AB_ENABLED'] = 'true'
        os.environ['JEV_AB_DIR'] = self.tmp
        os.environ['TYPESAFE_API_KEY'] = KEY
        calls = []
        inp = {'businesses': [biz()], 'evidence': []}
        out = je.owner_screen(self.tmp, inp, {'country': 'ID'},
                              lambda *a: calls.append(a))
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    # ---- 8. owner positives/uncertain ALWAYS Hermes-verified ----

    def test_positive_or_uncertain_owner_goes_to_hermes(self):
        os.environ['JEV_AB_ENABLED'] = 'true'
        os.environ['JEV_AB_DIR'] = self.tmp
        os.environ['TYPESAFE_API_KEY'] = KEY
        import jev_shadow
        got = {}
        def rm(run, role, inputs, spec):
            got['businesses'] = [b['business_id'] for b in inputs['businesses']]
            return {'resolutions': [{'business_id': BID, 'field': 'owner', 'status': 'not_found',
                                     'value': None, 'confidence': 0, 'evidence_ids': [],
                                     'resolver': 'resolve-owner', 'retrieved_at': 'x',
                                     'resolved_at': 'x', 'method': 'hermes'}]}
        for p in [0.95, 0.90, 0.50, 0.11]:
            with self.subTest(p=p):
                with mock.patch.object(jev_shadow, '_post',
                                       side_effect=[{'value': 0.05, 'input_tokens': 1, 'output_tokens': 1},
                                                    {'value': p, 'input_tokens': 1, 'output_tokens': 1}]):
                    inp = {'businesses': [biz(BID), biz(BID2)],
                           'evidence': [owner_evidence_row(BID, 'no owner mention here', 'e_1'),
                                        owner_evidence_row(BID2, 'Ani founded this spa', 'e_2')]}
                    out = je.owner_screen(self.tmp, inp, {'country': 'ID'}, rm)
                # BID screens out (p=0.05 confident-NO); BID2's p is
                # positive/uncertain -> Hermes verification mandatory.
                self.assertEqual(got['businesses'], [BID2])
                self.assertEqual(len(out['resolutions']), 2)
                self.assertFalse(any(r.get('method', '').startswith('Jev live A/B screening')
                                     for r in out['resolutions']
                                     if r['business_id'] == BID2))

    # ---- 9. Maps triage: pre-filter only, cannot bypass identity rules ----

    def test_maps_rescue_preselects_confident_unique_and_reverifies(self):
        os.environ['JEV_AB_ENABLED'] = 'true'
        os.environ['JEV_AB_DIR'] = self.tmp
        os.environ['TYPESAFE_API_KEY'] = KEY
        import jev_shadow, quality
        cands = [('Test Spa Bali', 'e1'), ('Test Spa Bali Ubud', 'e2')]
        lines = '\n'.join(f'- link "{n}" [ref={ref}]' for n, ref in cands)
        page = {'untrusted_page_text': lines, 'url': 'https://www.google.com/maps/search/spa?hl=en'}
        class FakeBrowser:
            # browser.open/link return JSON STRINGS (quality.page/target
            # json.loads the result), matching the real browser contract.
            def open(self, args):
                return json.dumps(page)
            def link(self, args):
                return json.dumps({'untrusted_page_text':
                                   'x\nfinal -> https://www.google.com/maps/place/selected'})
        verified = {}
        def fake_maps_listing(browser, b, spec):
            verified['url'] = b['url']
            return {'untrusted_page_text': 'heading "Test Spa Bali" [level=1]', 'url': b['url']}
        with mock.patch.object(quality, 'maps_listing', side_effect=fake_maps_listing), \
             mock.patch.object(jev_shadow, '_post',
                               side_effect=[{'value': 0.90, 'input_tokens': 1, 'output_tokens': 1},
                                            {'value': 0.05, 'input_tokens': 1, 'output_tokens': 1}]):
            out = je.maps_rescue(self.tmp, FakeBrowser(),
                                 {'business_id': BID, 'name_raw': 'Test Spa Bali', 'url': ''},
                                 {'area': 'Canggu, Bali'},
                                 ValueError('Exact Maps business listing not uniquely identified'))
        self.assertIsNotNone(out)
        self.assertIn('/maps/place/', verified['url'])  # re-verified by maps_listing
        recs = self.records()
        self.assertEqual(recs[0]['decision'], 'pre_selected')
        self.assertEqual(recs[0]['candidates'], ['Test Spa Bali', 'Test Spa Bali Ubud'])

    def test_maps_rescue_falls_back_unless_confident_and_unique(self):
        os.environ['JEV_AB_ENABLED'] = 'true'
        os.environ['JEV_AB_DIR'] = self.tmp
        os.environ['TYPESAFE_API_KEY'] = KEY
        import jev_shadow, quality
        cands = [('Test Spa Bali', 'e1'), ('Test Spa Bali Ubud', 'e2')]
        lines = '\n'.join(f'- link "{n}" [ref={ref}]' for n, ref in cands)
        page = {'untrusted_page_text': lines, 'url': 'https://www.google.com/maps/search/spa?hl=en'}
        class FakeBrowser:
            def open(self, args):
                return page
        for ps in [[0.90, 0.85], [0.10, 0.05], [0.55, 0.05]]:
            with self.subTest(ps=ps):
                with mock.patch.object(quality, 'maps_listing',
                                       side_effect=AssertionError('must not be reached')), \
                     mock.patch.object(jev_shadow, '_post',
                                       side_effect=[{'value': p, 'input_tokens': 1, 'output_tokens': 1} for p in ps]):
                    out = je.maps_rescue(self.tmp, FakeBrowser(),
                                         {'business_id': BID, 'name_raw': 'Test Spa Bali', 'url': ''},
                                         {'area': 'Canggu, Bali'},
                                         ValueError('Exact Maps business listing not uniquely identified'))
                self.assertIsNone(out)  # deterministic error path preserved

    def test_maps_rescue_ignores_non_identity_errors(self):
        os.environ['JEV_AB_ENABLED'] = 'true'
        class FakeBrowser:
            pass
        self.assertIsNone(je.maps_rescue('/tmp/x', FakeBrowser(), biz(), {'area': 'a'},
                                         ValueError('Maps listing name/URL identity was not verified')))

    # ---- 10-12. validator/supervisor untouched; env pass-through explicit ----

    def test_validator_supervisor_provenance_code_has_no_experiment_hook(self):
        import pipeline
        for fn in ['validate_all', 'consolidate', 'gate', 'render', 'deterministic_resolutions']:
            self.assertNotIn('jev', inspect.getsource(getattr(pipeline, fn)).lower())

    def test_env_passthrough_explicit_and_additive(self):
        import pipeline
        src = inspect.getsource(pipeline.launch_worker)
        for k in ["'JEV_AB_ENABLED'", "'JEV_AB_OWNER'", "'JEV_AB_MAPS'",
                  "'JEV_AB_OWNER_T'", "'JEV_AB_MAPS_T'", "'JEV_AB_DIR'"]:
            self.assertIn(k, src)

    def test_worker_resolve_flag_off_calls_hermes_directly(self):
        import worker
        called = {}
        def rm(run, role, inputs, spec):
            called['n'] = called.get('n', 0) + 1
            self.assertEqual(inputs['businesses'], [biz()])  # unmodified batch
            return {'resolutions': []}
        with mock.patch.object(worker, 'resolve_model', side_effect=rm):
            out = worker.resolve('/tmp/x', 'resolve-owner',
                                 {'businesses': [biz()],
                                  'evidence': [owner_evidence_row(BID, 'Owned by Ani', 'e_1')]},
                                 {'country': 'ID'})
        self.assertEqual(out, {'resolutions': []})
        self.assertEqual(called.get('n'), 1)

    def test_key_never_logged_in_records(self):
        os.environ['JEV_AB_ENABLED'] = 'true'
        os.environ['JEV_AB_DIR'] = self.tmp
        os.environ['TYPESAFE_API_KEY'] = KEY
        import jev_shadow
        with mock.patch.object(jev_shadow, '_post',
                               return_value={'value': 0.05, 'input_tokens': 1, 'output_tokens': 1}):
            inp = {'businesses': [biz()], 'evidence': [owner_evidence_row(BID, 'Spa', 'e_1')]}
            je.owner_screen(self.tmp, inp, {'country': 'ID'}, lambda *a: {'resolutions': []})
        for p in Path(self.tmp).glob('*'):
            self.assertNotIn(KEY, p.read_text())

    def test_probe_roles_still_blocked(self):
        import pipeline
        self.assertIn("raise PermissionError('Probe/out-of-scope role disabled')",
                      inspect.getsource(pipeline.execute))


def pytest_fail():
    raise AssertionError('screened business must skip Hermes')


if __name__ == '__main__':
    unittest.main()
