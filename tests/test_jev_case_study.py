# Experiment-only tests for the Jev case study: mandated safety properties.
# No network calls, no API key access. Production modules are untouched.
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
EXP = HERE.parent / 'experiments' / 'jev_case_study'
if not EXP.is_dir():
    raise unittest.SkipTest('private Jev case-study sources are not part of the open-source tree')
sys.path.insert(0, str(EXP))
sys.path.insert(0, str(HERE.parent / 'app'))

import flags  # noqa: E402
import routing  # noqa: E402
import dataset as D  # noqa: E402

FLAG_VARS = [k for k in os.environ if k.startswith('JEV_CASE_STUDY')]


def clear_flags():
    saved = {k: os.environ[k] for k in FLAG_VARS if k in os.environ}
    for k in list(os.environ):
        if k.startswith('JEV_CASE_STUDY'):
            os.environ.pop(k, None)
    return saved


def restore_flags(saved):
    os.environ.update(saved)


def biz(bid='b_x', name='Spa Test'):
    return {'business_id': bid, 'name_raw': name}


def ev(**kw):
    base = {'evidence_id': 'e_' + kw.get('id', 'x'), 'business_id': 'b_x',
            'field': 'whatsapp', 'source_kind': 'official_site', 'reader': 'read-site',
            'source_url': 'https://example.com/', 'snippet': '', 'raw_text': ''}
    base.update(kw)
    return base


class Flags(unittest.TestCase):
    def setUp(self): self.saved = clear_flags()
    def tearDown(self): restore_flags(self.saved)

    def test_master_flag_defaults_false(self):
        self.assertFalse(flags.enabled())
        for f in flags.FIELDS:
            self.assertFalse(flags.field_enabled(f))

    def test_subflags_default_false(self):
        os.environ['JEV_CASE_STUDY_ENABLED'] = 'true'
        self.assertTrue(flags.enabled())
        for f in flags.FIELDS:
            self.assertFalse(flags.field_enabled(f))

    def test_flags_require_exact_true(self):
        os.environ['JEV_CASE_STUDY_ENABLED'] = 'True'
        os.environ['JEV_CASE_STUDY_OWNER'] = '1'
        self.assertFalse(flags.enabled())
        self.assertFalse(flags.field_enabled('owner'))


class Dataset(unittest.TestCase):
    def test_frozen_dataset_reproducible(self):
        d = D.load()  # verifies sha256 internally
        self.assertEqual(len(d['businesses']), 10)
        self.assertEqual(len({b['business_id'] for b in d['businesses']}), 10)
        sha_file = (EXP / 'results' / 'case-study-set.sha256').read_text().split()[0]
        actual = hashlib.sha256((EXP / 'results' / 'case-study-set.json').read_bytes()).hexdigest()
        self.assertEqual(sha_file, actual)

    def test_control_and_treatment_share_identical_evidence(self):
        d = D.load()
        for sel in d['selection']:
            rows = [e for e in d['evidence'] if e['business_id'] == sel['business_id']]
            self.assertTrue(rows, sel)
            for e in rows:
                self.assertLessEqual({'evidence_id', 'business_id', 'field', 'snippet',
                                      'snapshot_id', 'snapshot_sha256', 'source_url'} - set(e), set())


class Safety(unittest.TestCase):
    def test_judgment_fields_only(self):
        self.assertEqual(set(routing.CASES), {'instagram', 'whatsapp', 'owner'})
        src = (EXP / 'routing.py').read_text()
        for forbidden in ('business_name', 'google_rating', 'google_review_count',
                          'public_contact'):
            self.assertNotIn(f"'{forbidden}':", src)

    def test_jev_arm_never_browses_or_messages(self):
        src = (EXP / 'routing.py').read_text()
        self.assertNotIn('Browser', src)
        self.assertNotIn('subprocess', src)
        self.assertNotIn('socket', src)
        csrc = (EXP.parent / 'jev' / 'client.py').read_text()
        self.assertIn('urllib.request', csrc)
        self.assertNotIn('subprocess', csrc)
        # The adapter performs a single POST; it can never GET/browse.
        self.assertIn("method='POST'", csrc)

    def test_production_modules_do_not_import_case_study(self):
        for py in (HERE.parent / 'app').glob('*.py'):
            src = py.read_text()
            self.assertNotIn('jev_case_study', src, py.name)
            self.assertNotIn('JEV_CASE_STUDY', src, py.name)

    def test_no_production_canonical_writes(self):
        for py in EXP.glob('*.py'):
            src = py.read_text()
            for forbidden in ("'canonical'", '"canonical"', "'resolved'", '"resolved"',
                              "'deliverables'", '"deliverables"'):
                self.assertNotIn(forbidden, src, f'{py.name} references {forbidden}')

    def test_api_key_never_logged(self):
        csrc = (EXP.parent / 'jev' / 'client.py').read_text()
        # The key is used exactly once, in the Authorization header.
        self.assertEqual(csrc.count("'Authorization': 'Bearer ' + key"), 1)
        # The adapter never writes anything to disk (so it can never persist the key).
        self.assertNotIn(".write", csrc.replace("write_text", ""))
        rsrc = (EXP / 'routing.py').read_text()
        # The routing layer never sees the key at all.
        self.assertNotIn('TYPESAFE_API_KEY', rsrc)
        self.assertNotIn('print(', rsrc)
        self.assertNotIn('log', rsrc.lower().replace('login', ''))

    def test_threshold_is_marked_case_study_only(self):
        self.assertIn('CASE-STUDY ONLY', (EXP / 'routing.py').read_text())
        self.assertIn('CASE-STUDY ONLY', (EXP / 'metrics.py').read_text())


class WhatsAppRouting(unittest.TestCase):
    def test_explicit_link_stays_deterministic(self):
        e = [ev(id='1', snippet='Chat on WhatsApp https://wa.me/6281234567890')]
        case = routing.whatsapp_case(biz(), e, 'ID')
        self.assertEqual(case['route'], 'deterministic_decisive')
        self.assertFalse(case['jev_called'])

    def test_phone_alone_is_not_whatsapp_and_jev_cannot_mint_value(self):
        e = [ev(id='1', snippet='Call us +62 812 3456 7890'),
             ev(id='2', field='whatsapp', snippet='Chat with us!')]
        with mock.patch.object(routing, 'system_one') as m:
            ans = {'wa_0': mock.Mock(value=1.0, probabilities={}, confidence=1.0),
                   'wa_1': mock.Mock(value=1.0, probabilities={}, confidence=1.0)}
            m.return_value = mock.Mock(answers=ans, latency_s=0.01)
            case = routing.whatsapp_case(biz(), e, 'ID')
        self.assertEqual(case['route'], 'jev_judged')
        self.assertEqual(case.get('explicit_values', case.get('explicit')) or [], [])
        for r in case['jev_rows']:
            self.assertFalse(r['would_create_value'])


class InstagramRouting(unittest.TestCase):
    def test_jev_cannot_bypass_corroboration(self):
        from quality import instagram_support
        e = [
            ev(id='1', field='website', source_kind='google_maps', reader='read-google',
               source_url='https://www.google.com/maps/place/x',
               snippet='site https://spa.com'),
            ev(id='2', field='instagram_candidate', source_kind='official_site',
               source_url='https://spa.com', snippet='Follow us https://www.instagram.com/spaofficial/'),
            ev(id='3', field='instagram_candidate', source_kind='official_site',
               source_url='https://spa.com', snippet='Follow us https://www.instagram.com/spaofficial2/'),
        ]
        self.assertGreaterEqual(len(instagram_support('b_x', e)), 2)
        with mock.patch.object(routing, 'system_one') as m:
            ans = {'ig': mock.Mock(type='choice', value='https://www.instagram.com/spaofficial/',
                                   probabilities={'https://www.instagram.com/spaofficial/': 0.99},
                                   confidence=0.99)}
            m.return_value = mock.Mock(answers=ans, latency_s=0.01)
            case = routing.instagram_case(biz(), e)
        self.assertTrue(case['jev_called'])
        self.assertFalse(case['corroboration_would_accept'])
        self.assertTrue(case['fallback'])


class OwnerRouting(unittest.TestCase):
    def _owner_ev(self, snippet):
        return [ev(id='1', field='owner', reader='find-owner', source_kind='official_site',
                   snippet=snippet)]

    def test_positive_requires_hermes_verification(self):
        with mock.patch.object(routing, 'system_one') as m:
            ans = {'owner_0': mock.Mock(value=0.9, probabilities={}, confidence=None)}
            m.return_value = mock.Mock(answers=ans, latency_s=0.01)
            case = routing.owner_case(biz(), self._owner_ev('Contact our manager John'))
        self.assertTrue(case['jev_positive'])
        self.assertTrue(case['fallback'])  # Hermes verification mandatory

    def test_negative_is_not_a_value(self):
        with mock.patch.object(routing, 'system_one') as m:
            ans = {'owner_0': mock.Mock(value=0.1, probabilities={}, confidence=None)}
            m.return_value = mock.Mock(answers=ans, latency_s=0.01)
            case = routing.owner_case(biz(), self._owner_ev('Founded by two sisters'))
        self.assertFalse(case['jev_positive'])
        self.assertFalse(case['fallback'])
        self.assertLess(case['jev_rows'][0]['jev_value'], 0.5)

    def test_api_failure_falls_back_safely(self):
        with mock.patch.object(routing, 'system_one', side_effect=RuntimeError('boom')):
            case = routing.owner_case(biz(), self._owner_ev('Owner: Jane'))
        self.assertEqual(case['route'], 'jev_error_fallback')
        self.assertTrue(case['fallback'])
        self.assertIn('boom', case['jev_error'])

    def test_missing_key_falls_back_safely(self):
        saved = os.environ.pop('TYPESAFE_API_KEY', None)
        keyfile = EXP.parent / 'jev' / '.key'
        hidden = keyfile.with_suffix('.hidden')
        moved = keyfile.exists()
        try:
            if moved:
                keyfile.rename(hidden)
            from client import MissingAPIKeyError
            with mock.patch.object(routing, 'system_one',
                                   side_effect=MissingAPIKeyError('no key')):
                case = routing.owner_case(biz(), self._owner_ev('Owner: Jane'))
            self.assertEqual(case['route'], 'jev_error_fallback')
            self.assertTrue(case['fallback'])
        finally:
            if saved:
                os.environ['TYPESAFE_API_KEY'] = saved
            if moved:
                hidden.rename(keyfile)


if __name__ == '__main__':
    unittest.main()
