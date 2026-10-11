# Jev SHADOW MODE tests — prove zero effect on the pipeline and secret safety.
import ast
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import jev_shadow as JS  # noqa: E402

KEY = 'test-shadow-key-abc123'


def ok_post(value=0.9):
    return {'value': value, 'model': 'jev-1.0', 'input_tokens': 10, 'output_tokens': 1}


class ShadowEnv(unittest.TestCase):
    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.run = Path(self._dirs.name) / 'run'
        self.run.mkdir()
        self._patches = [mock.patch.dict(os.environ, {
            'JEV_SHADOW_ENABLED': 'true', 'TYPESAFE_API_KEY': KEY,
            'JEV_SHADOW_DIR': str(self.run / 'shadow' / 'jev'),
        })]
        for p in self._patches:
            p.start()
        self.addCleanup(self._dirs.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)


class Defaults(ShadowEnv):
    def test_flag_default_false_makes_zero_calls(self):
        # 1. JEV_SHADOW_ENABLED=false (and unset) makes zero API calls.
        for setting in ('false', None):
            if setting is None:
                os.environ.pop('JEV_SHADOW_ENABLED', None)
            else:
                os.environ['JEV_SHADOW_ENABLED'] = 'false' if setting == 'false' else setting
            with mock.patch.object(JS, '_post', side_effect=AssertionError('API call made while disabled')):
                self.assertIsNone(JS._collect(self.run, 'owner', 'b1', 'e1', 'text', 'find-owner', None, None))
                self.assertIsNone(JS.collect_stage(self.run, 'resolve-owner', {'evidence': []}, {'resolutions': []}))
            os.environ.pop('JEV_SHADOW_ENABLED', None)
        self.assertFalse((self.run / 'shadow').exists())

    def test_subflag_owner_off_disables_owner_stream(self):
        os.environ['JEV_SHADOW_OWNER'] = 'false'
        with mock.patch.object(JS, '_post', side_effect=AssertionError('owner call made')):
            self.assertIsNone(JS._collect(self.run, 'owner', 'b1', 'e1', 't', 'find-owner', None, None))
        os.environ.pop('JEV_SHADOW_OWNER')
        with mock.patch.object(JS, '_post', return_value=ok_post()) as m:
            self.assertTrue(JS._collect(self.run, 'review_owner_reply', 'b1', 'e1', 't', 'read-reviews', None, None))
            self.assertEqual(m.call_count, 1)


class FailureIsolation(ShadowEnv):
    def test_missing_key_never_affects_pipeline_result(self):
        # 2. Missing TYPESAFE_API_KEY: error record only; resolution untouched.
        os.environ.pop('TYPESAFE_API_KEY')
        resolution = {'business_id': 'b1', 'field': 'owner', 'status': 'not_found'}
        with mock.patch.object(JS, '_post', side_effect=AssertionError('no key must mean no call')):
            self.assertIsNone(JS.collect_stage(self.run, 'resolve-owner',
                                               {'evidence': [{'business_id': 'b1', 'evidence_id': 'e1', 'field': 'owner', 'snippet': 'x'}]},
                                               {'resolutions': [resolution]}))
        self.assertEqual(resolution['status'], 'not_found')
        errs = (self.run / 'shadow/jev/errors.jsonl').read_text()
        self.assertIn('MissingAPIKey', errs)

    def _assert_no_raise(self, exc):
        with mock.patch.object(JS, '_post', side_effect=exc):
            self.assertIsNone(JS._collect(self.run, 'owner', 'b1', 'e1', 't', 'find-owner', None, {'status': 'not_found'}))

    def test_all_failure_modes_swallowed(self):
        # 3+4. API 4xx/5xx, timeout, transport, malformed: never raise, error records only.
        for exc in [urllib.error.HTTPError(JS.API_URL, 500, 'boom', None, None),
                    urllib.error.HTTPError(JS.API_URL, 401, 'unauthorized', None, None),
                    TimeoutError('timed out'),
                    urllib.error.URLError('connection refused'),
                    OSError('network down'),
                    ValueError('malformed shadow response: missing noul')]:
            before = (self.run / 'shadow/jev/errors.jsonl').read_text() if (self.run / 'shadow/jev/errors.jsonl').exists() else ''
            self._assert_no_raise(exc)
            after = (self.run / 'shadow/jev/errors.jsonl').read_text()
            self.assertTrue(len(after) > len(before))

    def test_timeout_never_triggers_semantic_retry(self):
        # 4. The pipeline hook swallows a timeout: no exception can reach the
        # worker's failure classification, so no semantic retry can be caused.
        def boom(run, role, inputs, out):
            JS.collect_stage(run, role, inputs, out)
        with mock.patch.object(JS, '_post', side_effect=TimeoutError('timed out')):
            self.assertIsNone(boom(self.run, 'resolve-owner', {'evidence': []}, {'resolutions': []}))
        # Structural: the hook site discards the return value and is outside
        # worker_main's failure classification.
        src = (ROOT / 'app/pipeline.py').read_text()
        self.assertIn('jev_shadow.collect_stage(run,role,inputs,out)', src)
        self.assertNotIn('collect_stage', src[src.find('def worker_main'):src.find('def stop_worker')])

    def test_shadow_never_enters_canonical_paths(self):
        # 5. Only shadow/ files are created; canonical dirs untouched.
        manifest = {p: p.stat().st_mtime_ns for p in self.run.rglob('*') if p.is_file()}
        with mock.patch.object(JS, '_post', return_value=ok_post()):
            JS._collect(self.run, 'owner', 'b1', 'e1', 't', 'find-owner', None, None)
        new = {str(p) for p in self.run.rglob('*') if p.is_file() and p not in manifest}
        self.assertTrue(all('/shadow/jev/' in p for p in new))
        for sub in ('raw', 'resolved', 'canonical', 'control', 'deliverables'):
            self.assertFalse(any(f'/{sub}/' in p for p in new), new)

    def test_resolver_output_and_inputs_unmodified(self):
        # 6. collect_stage mutates neither inputs nor out; resolver output equal
        # with shadow enabled+mocked vs disabled.
        import copy
        from core import resolution
        from worker import resolve
        ev = [{'evidence_id': 'e1', 'business_id': 'b1', 'reader': 'read-reviews',
               'source_kind': 'google_maps', 'field': 'owner_reply',
               'snippet': 'Response from the owner a month ago', 'retrieved_at': '2026-09-23T00:00:00+00:00'},
              {'evidence_id': 'e2', 'business_id': 'b1', 'reader': 'read-ig',
               'source_kind': 'instagram', 'field': 'posting_recency',
               'snippet': 'Sign up and never miss a post', 'retrieved_at': '2026-09-23T00:00:00+00:00'}]
        businesses = [{'business_id': 'b1'}]
        spec = {'requested_fields': ['dm_proxy'], 'country': 'ID'}
        r = resolution('b1', 'dm_proxy', 'found', {'label': 'x', 'signals': {}, 'score': None}, ['e1'], ev, 'm', 0.5)
        r['components'] = {'owner_reply': {'status': 'observed', 'evidence_ids': ['e1']}, 'posting_recency': {'status': 'unavailable', 'evidence_ids': []}}
        out = {'resolutions': [r]}
        inputs = {'businesses': businesses, 'evidence': ev}
        snap_in, snap_out = copy.deepcopy(inputs), copy.deepcopy(out)
        with mock.patch.object(JS, '_post', return_value=ok_post(0.99)) as m:
            JS.collect_stage(self.run, 'resolve-dm', inputs, out)
            self.assertEqual(m.call_count, 1)  # posting_recency row not shadowed
            with mock.patch.dict(os.environ, {'JEV_SHADOW_ENABLED': 'false'}):
                out2 = resolve(Path(self.run), 'resolve-dm', inputs, spec)
            with mock.patch.dict(os.environ, {'JEV_SHADOW_ENABLED': 'true'}):
                out1 = resolve(Path(self.run), 'resolve-dm', inputs, spec)
        strip_ts = lambda rows: [{k: v for k, v in r.items() if k not in ('resolved_at', 'retrieved_at')} for r in rows]
        self.assertEqual(json.dumps(strip_ts(out1['resolutions']), sort_keys=True),
                         json.dumps(strip_ts(out2['resolutions']), sort_keys=True))
        self.assertEqual(json.dumps(inputs, sort_keys=True), json.dumps(snap_in, sort_keys=True))
        self.assertEqual(json.dumps(out, sort_keys=True), json.dumps(snap_out, sort_keys=True))

    def test_no_reference_from_validator_supervisor_guard_quality(self):
        # 7+8. Validator/Supervisor/guard/quality/core never reference shadow.
        for mod in ['core', 'quality', 'guard', 'browser', 'chrome', 'worker']:
            src = (ROOT / 'app' / f'{mod}.py').read_text()
            self.assertNotIn('jev_shadow', src)
            self.assertNotIn('shadow/jev', src)
        src = (ROOT / 'app/pipeline.py').read_text()
        refs = [(i, l.strip()) for i, l in enumerate(src.splitlines()) if 'jev_shadow' in l]
        self.assertEqual(len(refs), 2)  # exactly the hook import + call
        self.assertIn('import jev_shadow', refs[0][1])
        self.assertEqual(refs[1][1], 'jev_shadow.collect_stage(run,role,inputs,out)')
        # the call sits in the resolve-stage branch only
        self.assertIn("role in {'resolve-owner','resolve-dm'}", src)
        # validator/supervisor branches contain no shadow reference
        v = src[src.find("elif role=='validator'"):src.find("elif role.startswith('render-')")]
        self.assertNotIn('jev_shadow', v)

    def test_owner_question_uses_preserved_evidence_only(self):
        # 9. State carries only this evidence row's text (+ name/dm_probe false).
        captured = {}
        def fake_post(state, question, key):
            captured.update(state=state, question=question)
            return ok_post(0.9)
        with mock.patch.object(JS, '_post', side_effect=fake_post):
            JS._collect(self.run, 'owner', 'b1', 'e1', 'Jane Doe - Spa Manager', 'find-owner',
                        'https://example.com/about', {'status': 'not_found', 'value': None}, 'Spa X')
        self.assertEqual(set(captured['state']), {'business_name', 'evidence_text', 'dm_probe'})
        self.assertEqual(captured['state']['evidence_text'], 'Jane Doe - Spa Manager')
        self.assertIs(captured['state']['dm_probe'], False)
        self.assertEqual(captured['question']['instructions']['question'],
                         'Does this evidence explicitly identify the named person as an owner, founder, or co-founder of this business?')

    def test_review_question_one_row_per_call(self):
        # 10. One review row per call; posting_recency rows are never shadowed.
        calls = []
        def fake_post(state, question, key):
            calls.append(state['evidence_text'])
            return ok_post(0.1)
        ev = [{'evidence_id': 'e1', 'business_id': 'b1', 'field': 'owner_reply', 'snippet': 'Response from the owner', 'reader': 'read-reviews'},
              {'evidence_id': 'e2', 'business_id': 'b1', 'field': 'posting_recency', 'snippet': 'Sign up', 'reader': 'read-ig'},
              {'evidence_id': 'e3', 'business_id': 'b1', 'field': 'owner_reply', 'snippet': 'Tanggapan dari pemilik', 'reader': 'read-reviews'}]
        with mock.patch.object(JS, '_post', side_effect=fake_post):
            JS.collect_stage(self.run, 'resolve-dm', {'evidence': ev}, {'resolutions': []})
        self.assertEqual(calls, ['Response from the owner', 'Tanggapan dari pemilik'])
        self.assertIn('Does this specific review row contain a reply attributable to the business/owner account?',
                      JS.QUESTIONS['review_owner_reply']['instructions']['question'])


class SafetySurface(unittest.TestCase):
    def test_no_browser_capability(self):
        # 11. AST import scan: no browser/CDP/shell/network-client beyond urllib.
        tree = ast.parse((ROOT / 'app/jev_shadow.py').read_text())
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported.update(a.name.split('.')[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                imported.add((n.module or '').split('.')[0])
        forbidden = {'browser', 'chrome', 'tools', 'subprocess', 'socket', 'http',
                     'smtplib', 'playwright', 'selenium', 'ftplib', 'run_agent'}
        self.assertFalse(imported & forbidden, imported & forbidden)

    def test_no_outreach_capability(self):
        # 12. No messaging targets or send primitives anywhere in the module.
        src = (ROOT / 'app/jev_shadow.py').read_text()
        for token in ['wa.me', 'whatsapp', 'mailto', 'smtplib', 'sendmail',
                      'SENDMAIL', 'message/send', 'api.whatsapp', 'telegram']:
            self.assertNotIn(token, src.lower())


class Secrets(ShadowEnv):
    def test_key_never_logged(self):
        # 13. No logging at all; errors scrub the key defensively.
        with self.assertNoLogs(level='DEBUG'):
            with mock.patch.object(JS, '_post', side_effect=urllib.error.HTTPError(JS.API_URL, 401, 'unauthorized', None, None)):
                JS._collect(self.run, 'owner', 'b1', 'e1', 't', 'find-owner', None, None)
        rec = (self.run / 'shadow/jev/errors.jsonl').read_text()
        self.assertNotIn(KEY, rec)
        self.assertEqual(JS._scrub('Bearer ' + KEY + ' x', KEY), 'Bearer <redacted> x')

    def test_shadow_logs_contain_no_secret(self):
        # 14. Success + failure records both key-free.
        with mock.patch.object(JS, '_post', return_value=ok_post()):
            JS._collect(self.run, 'owner', 'b1', 'e1', 'evidence with ' + KEY + ' inside', 'find-owner', None, None)
        os.environ.pop('TYPESAFE_API_KEY')
        JS._collect(self.run, 'review_owner_reply', 'b1', 'e2', 't', 'read-reviews', None, None)
        for name in ('records.jsonl', 'errors.jsonl'):
            p = self.run / 'shadow/jev' / name
            self.assertTrue(p.exists())
            self.assertNotIn(KEY, p.read_text())

    def test_records_carry_required_fields(self):
        with mock.patch.object(JS, '_post', return_value=ok_post(0.87)):
            self.assertTrue(JS._collect(self.run, 'owner', 'b1', 'e1', 'text', 'find-owner',
                                        'https://example.com', {'status': 'found', 'value': 'Bob'}, 'Spa'))
        rec = json.loads((self.run / 'shadow/jev/records.jsonl').read_text().splitlines()[0])
        for field in ['at', 'run_id', 'business_id', 'evidence_id', 'decision_type',
                      'existing_result', 'jev_value', 'jev_confidence', 'source_role',
                      'evidence_text', 'latency_s']:
            self.assertIn(field, rec)
        self.assertEqual((rec['decision_type'], rec['jev_value']), ('owner', 0.87))
        self.assertEqual(rec['existing_result'], {'status': 'found', 'value': 'Bob'})


class SandboxAndEnv(unittest.TestCase):
    def test_policy_unchanged_when_flag_off(self):
        import sandbox
        from core import ROOT
        run = ROOT / 'runs/run-20260923-071023'
        os.environ.pop('JEV_SHADOW_ENABLED', None)
        pol = sandbox.policy(run, 'resolve-owner')
        self.assertNotIn('typesafe', pol)
        self.assertNotIn('shadow', pol)

    def test_policy_allows_shadow_egress_when_flag_on(self):
        import sandbox
        from core import ROOT
        run = ROOT / 'runs/run-20260923-071023'
        with mock.patch.dict(os.environ, {'JEV_SHADOW_ENABLED': 'true'}):
            pol = sandbox.policy(run, 'resolve-owner')
        self.assertIn('api.typesafe.ai:443', pol)
        self.assertIn(str(run / 'shadow'), pol)
        with mock.patch.dict(os.environ, {'JEV_SHADOW_ENABLED': 'true'}):
            pol_read = sandbox.policy(run, 'resolve-dm')
            pol_other = sandbox.policy(run, 'resolve-ig')
        self.assertIn('api.typesafe.ai:443', pol_read)
        self.assertNotIn('api.typesafe.ai:443', pol_other)  # only shadowed roles

    def test_env_passthrough_only_when_set(self):
        src = (ROOT / 'app/pipeline.py').read_text()
        self.assertIn("'JEV_SHADOW_ENABLED','JEV_SHADOW_OWNER','JEV_SHADOW_REVIEW_REPLY','JEV_SHADOW_DIR','TYPESAFE_API_KEY'", src)
        self.assertIn('if k in os.environ:env[k]=os.environ[k]', src)


if __name__ == '__main__':
    unittest.main()
