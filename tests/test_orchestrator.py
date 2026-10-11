from pathlib import Path
import json, os, shutil, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import orchestrator as o
from core import digest
import yaml

SMALL = 'Find 3 bookshops in Ubud, ID. No outreach.'
SPEC_KEYS = ['category', 'area', 'country', 'count', 'sources', 'requested_fields', 'dm_probe', 'formats']
GOOD_SPEC = {
    'category': 'bookshops',
    'area': 'Ubud',
    'country': 'ID',
    'count': 3,
    'sources': ['google_maps', 'web', 'instagram'],
    'requested_fields': ['business_name', 'instagram', 'whatsapp', 'public_contact', 'google_rating', 'google_review_count', 'dm_proxy', 'dm_measured', 'owner'],
    'dm_probe': False,
    'formats': ['md', 'xlsx', 'pdf'],
}


def tmp_module(self):
    self.tmp = Path(tempfile.mkdtemp()).resolve()
    self.addCleanup(shutil.rmtree, self.tmp, True)
    o.SPECS = self.tmp / 'specs' / 'generated'
    o.SPECS.mkdir(parents=True)
    o.STATE = self.tmp / 'orchestrator' / 'state.json'
    o.LOGS = self.tmp / 'orchestrator' / 'logs'
    o.LOGS.mkdir(parents=True)
    o.RUNS = self.tmp / 'runs'
    o.RUNS.mkdir()
    o.LEADGEN = self.tmp / 'leadgen'
    return self.tmp


def full_rows(spec, status='found'):
    out = []
    for i in range(spec['count']):
        for f in spec['requested_fields']:
            row = {
                'business_id': 'b_%02d' % (i % spec['count']),
                'field': f,
                'value': None,
                'status': status,
                'confidence': 0.5,
                'evidence_ids': [],
            }
            out.append(row)
    return out


def write_run(tmp, spec, run_name='run-test', rows=None, deliverables=(), metrics='', graph_roles=None):
    run = tmp / 'runs' / run_name
    for sub in ('control', 'canonical', 'deliverables', 'reports/failures'):
        (run / sub).mkdir(parents=True)
    spec_file = run / 'control' / 'spec.json'
    spec_file.write_text(json.dumps(spec))
    data = {
        'run_id': run.name,
        'spec': spec,
        'businesses': [
            {'business_id': 'b_%02d' % i, 'evidence_ids': ['e1'], 'name_raw': 'B %d' % i, 'url': 'https://b%d.example' % i}
            for i in range(spec['count'])
        ],
        'evidence': [],
        'resolved_fields': rows if rows is not None else [],
        'validation': {'valid': True, 'errors': [], 'checked_at': '2026-09-22T00:00:00+00:00'},
        'source_priority': {},
        'approved': False,
    }
    canonical = run / 'canonical' / 'leads.json'
    canonical.write_text(json.dumps(data, sort_keys=True, ensure_ascii=False))
    approval = {'approved': True, 'canonical_sha256': digest(data), 'at': 'now', 'coverage': {}}
    (run / 'control' / 'approval.json').write_text(json.dumps(approval))
    for fmt in deliverables:
        f = run / 'deliverables' / ('leads.' + fmt)
        f.write_text('deliverable')
    (run / 'reports' / 'run-metrics.jsonl').write_text(metrics)
    roles = graph_roles or ['intake', 'render-md']
    graph = {}
    for n, role in enumerate(roles):
        node = {'id': 't_%d' % n, 'role': role, 'parents': []}
        graph['t_%d' % n] = node
    (run / 'control' / 'graph.json').write_text(json.dumps(graph))
    return run


def rewrite_canonical(run, mutate):
    data = json.loads((run / 'canonical' / 'leads.json').read_text())
    mutate(data)
    (run / 'canonical' / 'leads.json').write_text(json.dumps(data, sort_keys=True, ensure_ascii=False))
    approval = {'approved': True, 'canonical_sha256': digest(data), 'coverage': {}}
    (run / 'control' / 'approval.json').write_text(json.dumps(approval))
    return data


class ParseTests(unittest.TestCase):
    def test_1_request_to_intent(self):
        r = o.parse('Find 60 spa, wellness and massage businesses in Canggu, Pererenan, Berawa and Seminyak, Bali, ID. I need Instagram, WhatsApp, public contact, Google rating, Google review count, owner and DM responsiveness proxy. No outreach. Give me Markdown, Excel and PDF.')
        self.assertEqual(r['flags']['missing'], [])
        self.assertEqual(r['intent']['category'], 'spa wellness massage')
        self.assertEqual(r['intent']['area'], 'Canggu, Pererenan, Berawa, Seminyak, Bali')
        self.assertEqual(r['intent']['count'], 60)
        self.assertEqual(r['intent']['dm_probe'], False)
        self.assertEqual(r['intent']['sources'], ['google_maps', 'web', 'instagram'])
        self.assertEqual(len(r['intent']['requested_fields']), 8)
        self.assertEqual(r['intent']['requested_fields'],
            ['business_name', 'instagram', 'whatsapp', 'public_contact', 'google_rating', 'google_review_count', 'owner', 'dm_proxy'])
        self.assertEqual(r['intent']['formats'], ['md', 'xlsx', 'pdf'])

    def test_1b_missing_params_reported_not_asked(self):
        r = o.parse('Find bookshops.')
        self.assertIn('count', r['flags']['missing'])
        self.assertIn('area', r['flags']['missing'])
        self.assertIn('category', r['flags']['missing'])
        self.assertIn('country', r['flags']['missing'])
        self.assertIsNone(r['intent']['category'])

    def test_2_intent_to_yaml(self):
        tmp_module(self)
        r = o.spec_command(SMALL)
        self.assertEqual(r['status'], 'ok')
        spec_path = Path(r['spec_path'])
        self.assertTrue(str(spec_path).startswith(str(o.SPECS)))
        data = yaml.safe_load(spec_path.read_text())
        self.assertEqual(list(data), SPEC_KEYS)
        self.assertEqual(data['dm_probe'], False)
        self.assertEqual(data['count'], 3)
        meta_path = str(spec_path)[:-5] + '.meta.json'
        meta = json.loads(Path(meta_path).read_text())
        self.assertEqual(meta['request'], SMALL)
        self.assertEqual(meta['intent'], data)
        self.assertTrue(str(meta['spec_path']).startswith(str(o.SPECS)))
        self.assertIn('Outreach: Disabled', r['summary'])
        self.assertIn('Research request normalized:', r['summary'])

    def test_3_dm_probe_always_false(self):
        for text in (
            'Message the 20 dentists in Ubud on WhatsApp',
            'Find 3 bookshops in Ubud and dm them',
            'Find 5 cafes in Denpasar, send a WhatsApp to each',
        ):
            self.assertFalse(o.parse(text)['intent']['dm_probe'])
        tmp_module(self)
        refused = o.spec_command('Message the 20 dentists in Ubud on WhatsApp.')
        self.assertEqual(refused['status'], 'refused')
        intent = json.loads(json.dumps(o.parse(SMALL)['intent']))
        intent['dm_probe'] = True
        with self.assertRaisesRegex(ValueError, 'dm_probe must be false'):
            o.validate_intent(intent)

    def test_4_invalid_source_rejected(self):
        intent = json.loads(json.dumps(o.parse(SMALL)['intent']))
        intent['sources'] = ['twitter']
        with self.assertRaises(Exception):
            o.validate_intent(intent)
        with self.assertRaisesRegex(ValueError, 'google_maps or web'):
            o.validate_intent(dict(intent, sources=['instagram']))

    def test_5_invalid_field_rejected(self):
        intent = json.loads(json.dumps(o.parse(SMALL)['intent']))
        intent['requested_fields'] = ['email']
        with self.assertRaises(Exception):
            o.validate_intent(intent)

    def test_6_invalid_format_rejected(self):
        intent = json.loads(json.dumps(o.parse(SMALL)['intent']))
        intent['formats'] = ['csv']
        with self.assertRaises(Exception):
            o.validate_intent(intent)

    def test_7_count_validation(self):
        base = json.loads(json.dumps(o.parse(SMALL)['intent']))
        with self.assertRaisesRegex(ValueError, 'count must be between 1 and 60'):
            o.validate_intent(dict(base, count=0))
        with self.assertRaisesRegex(ValueError, 'count must be between 1 and 60'):
            o.validate_intent(dict(base, count=61))
        with self.assertRaisesRegex(ValueError, 'count must be between 1 and 60'):
            o.validate_intent(dict(base, count=-5))
        with self.assertRaisesRegex(ValueError, 'integer'):
            o.validate_intent(dict(base, count='3'))
        # Verified scale promoted to 60 after the PASS 60-business acceptance
        # run (run-20260923-071023): 60 accepted, normal sizes still accepted.
        self.assertEqual(o.validate_intent(dict(base, count=60))['count'], 60)
        self.assertEqual(o.validate_intent(dict(base, count=20))['count'], 20)

    def test_7b_spec_at_verified_limit(self):
        # The mandated smoke request parses fully and yields a valid 60-count
        # spec with the standard field set, md/xlsx/pdf and dm_probe false.
        tmp_module(self)
        r = o.spec_command('Find 60 spa, wellness and massage businesses in Canggu, Pererenan, Berawa and Seminyak, Bali, ID. No outreach. Generate Markdown, Excel and PDF.')
        self.assertEqual(r['status'], 'ok')
        spec_path = Path(r['spec_path'])
        data = yaml.safe_load(spec_path.read_text())
        self.assertEqual(list(data), SPEC_KEYS)
        self.assertEqual(data['count'], 60)
        self.assertEqual(data['dm_probe'], False)
        self.assertEqual(data['formats'], ['md', 'xlsx', 'pdf'])
        self.assertEqual(data['requested_fields'], list(o.FIELDS))
        self.assertEqual(data['sources'], list(o.SOURCES))
        self.assertTrue(o.validate_intent(json.loads(json.dumps(data)))['count'] == 60)
        meta = json.loads(Path(str(spec_path)[:-5] + '.meta.json').read_text())
        self.assertEqual(meta['intent'], data)

    def test_8_path_traversal_rejected(self):
        tmp_module(self)
        with self.assertRaisesRegex(ValueError, 'bare run directory'):
            o.status_command('../../etc')
        with self.assertRaisesRegex(ValueError, 'not found under runs/'):
            o.status_command('/etc')
        with self.assertRaisesRegex(ValueError, 'specs/generated'):
            o._require_generated_spec(self.tmp / 'specs' / 'example.yaml')

    def test_9_shell_injection_stays_inert_data(self):
        r = o.parse('Find 3 bookshops in Ubud, ID; rm -rf / && cat ~/.hermes/config.yaml')
        self.assertEqual(r['intent']['count'], 3)
        self.assertEqual(r['intent']['dm_probe'], False)
        yaml.safe_dump(r['intent'])
        src = Path(o.__file__).read_text()
        self.assertNotIn('shell=True', src)

    def test_10_generated_specs_stay_inside(self):
        tmp = tmp_module(self)
        r = o.spec_command(SMALL)
        self.assertTrue(str(r['spec_path']).startswith(str(o.SPECS)))
        outside = tmp / 'specs' / 'example.yaml'
        with self.assertRaisesRegex(ValueError, 'specs/generated'):
            o._require_generated_spec(outside)
        with self.assertRaisesRegex(ValueError, 'specs/generated'):
            o.launch_command(outside)

    def test_11_existing_specs_never_overwritten(self):
        tmp_module(self)
        existing = o.SPECS / 'leadgen-20260101-000000.yaml'
        existing.write_text('category: keep\n')
        meta = existing.parent / 'leadgen-20260101-000000.meta.json'
        meta.write_text(json.dumps({'request': 'original'}))
        for _ in range(3):
            r = o.spec_command(SMALL)
            self.assertNotEqual(Path(r['spec_path']), existing)
        self.assertEqual(existing.read_text(), 'category: keep\n')
        self.assertEqual(json.loads(meta.read_text())['request'], 'original')


class LaunchTests(unittest.TestCase):
    def test_12_cli_argv_fixed_non_shell(self):
        tmp = tmp_module(self)
        r = o.spec_command(SMALL)
        run_dir = tmp / 'runs' / 'run-y'
        (run_dir / 'control').mkdir(parents=True)
        spec_text = Path(r['spec_path']).read_text()
        (run_dir / 'control' / 'spec.json').write_text(spec_text)
        calls = []
        popen = mock.MagicMock()
        popen.pid = 4242
        popen.poll.return_value = None

        def fake_popen(argv, **kw):
            calls.append(list(argv))
            name = kw['stdout'].name
            kw['stdout'].close()
            with open(name, 'w') as fh:
                fh.write(str(run_dir) + '\n')
            return popen
        pre = mock.patch.object(o, 'preflight_command', return_value={'status': 'ok'})
        with mock.patch.object(o.subprocess, 'Popen', fake_popen), pre:
            out = o.launch_command(r['spec_path'])
        self.assertEqual(calls[0], [str(o.LEADGEN), 'start', r['spec_path']])
        self.assertEqual(out['status'], 'launched')
        self.assertEqual(out['run_id'], 'run-y')
        self.assertEqual(out['run'], str(run_dir))

    def test_13_run_id_captured_and_state_updated(self):
        tmp = tmp_module(self)
        r = o.spec_command(SMALL)
        run_dir = tmp / 'runs' / 'run-z'
        (run_dir / 'control').mkdir(parents=True)
        (run_dir / 'control' / 'spec.json').write_text(Path(r['spec_path']).read_text())
        popen = mock.MagicMock()
        popen.pid = 4243
        popen.poll.return_value = None

        def fake_popen(argv, **kw):
            name = kw['stdout'].name
            kw['stdout'].close()
            with open(name, 'w') as fh:
                fh.write(str(run_dir) + '\n')
            return popen
        pre = mock.patch.object(o, 'preflight_command', return_value={'status': 'ok'})
        with mock.patch.object(o.subprocess, 'Popen', fake_popen), pre:
            out = o.launch_command(r['spec_path'])
        self.assertEqual(out['run_id'], 'run-z')
        state = json.loads(o.STATE.read_text())
        self.assertEqual(state['last_run'], str(run_dir))
        meta = json.loads(Path(str(r['spec_path'])[:-5] + '.meta.json').read_text())
        self.assertEqual(meta['runs'][0]['run'], str(run_dir))


class MonitoringTests(unittest.TestCase):
    def _run(self, tmp, statuses, run_name='run-test'):
        run = write_run(tmp, GOOD_SPEC, run_name=run_name, graph_roles=['intake', 'query-plan', 'render-md'])
        tasks = []
        for tid, st in statuses.items():
            tasks.append({'id': tid, 'title': tid, 'status': st})
        (run / 'reports' / 'status.json').write_text(json.dumps({'at': 'now', 'tasks': tasks}))
        return run

    def test_14_status_parsing(self):
        tmp = tmp_module(self)
        run = self._run(tmp, {'t_0': 'done', 't_1': 'running', 't_2': 'ready'})
        o.STATE.write_text(json.dumps({'last_run': str(run), 'runs': [{'run': str(run), 'pid': os.getpid()}]}))
        out = o.status_command(run.name)
        self.assertEqual(out['state'], 'running')
        self.assertTrue(out['runner_alive'])
        stages = {}
        for p in out['progress']:
            stages[p['stage']] = (p['done'], p['total'])
        self.assertEqual(stages['intake'], (1, 1))
        self.assertEqual(stages['query-plan'], (0, 1))
        self.assertEqual(stages['render-md'], (0, 1))

    def test_15_hard_block_reporting(self):
        tmp = tmp_module(self)
        run = self._run(tmp, {'t_0': 'done', 't_1': 'blocked', 't_2': 'ready'})
        failure = {'task': 't_1', 'role': 'query-plan', 'category': 'LM_OUTPUT_INVALID', 'error': 'bad output', 'at': 'now'}
        (run / 'reports' / 'failures' / 't_1.json').write_text(json.dumps(failure))
        (run / 'control' / 'attempts.json').write_text(json.dumps({'t_1': 2}))
        (run / 'control' / 'infra-attempts.json').write_text(json.dumps({'t_1': 3}))
        out = o.failure_report_command(run.name)
        self.assertEqual(out['state'], 'blocked')
        b = out['blocked'][0]
        self.assertEqual(b['role'], 'query-plan')
        self.assertEqual(b['failure_category'], 'LM_OUTPUT_INVALID')
        self.assertEqual(b['attempts'], 2)
        self.assertEqual(b['infra_attempts'], 3)
        self.assertIn('recommended_next_diagnostic', b)
        self.assertIn('STOP', out['resolution'])
        self.assertIn('orchestrate resume', out['resolution'])
        self.assertEqual(o.status_command(run.name)['run_state'], 'failed')

    def test_15b_fail_report_infra_attempts_present(self):
        # STEP 6 case 'exists': valid infra-attempts.json is read and the
        # report states it explicitly.
        tmp = tmp_module(self)
        run = self._run(tmp, {'t_0': 'done', 't_1': 'blocked', 't_2': 'ready'})
        (run / 'reports' / 'failures' / 't_1.json').write_text(json.dumps({'task': 't_1', 'role': 'read-site', 'category': 'TASK_EXECUTION_FAILURE', 'error': 'x', 'at': 'now'}))
        (run / 'control' / 'attempts.json').write_text(json.dumps({'t_1': 2}))
        (run / 'control' / 'infra-attempts.json').write_text(json.dumps({'t_1': 3}))
        out = o.failure_report_command(run.name)
        self.assertEqual(out['state'], 'blocked')
        self.assertEqual(out['blocked'][0]['infra_attempts'], 3)
        self.assertEqual(out['infra_attempts_note'], '')

    def test_15c_fail_report_infra_attempts_absent(self):
        # STEP 6 case 'absent': a missing infra-attempts.json means zero
        # recorded infra attempts; the report stays valid and says so
        # explicitly instead of crashing.
        tmp = tmp_module(self)
        run = self._run(tmp, {'t_0': 'done', 't_1': 'blocked', 't_2': 'ready'})
        (run / 'reports' / 'failures' / 't_1.json').write_text(json.dumps({'task': 't_1', 'role': 'read-site', 'category': 'TASK_EXECUTION_FAILURE', 'error': 'x', 'at': 'now'}))
        (run / 'control' / 'attempts.json').write_text(json.dumps({'t_1': 2}))
        self.assertFalse((run / 'control' / 'infra-attempts.json').exists())
        out = o.failure_report_command(run.name)
        self.assertEqual(out['state'], 'blocked')
        self.assertEqual(len(out['blocked']), 1)
        self.assertIsNone(out['blocked'][0]['infra_attempts'])
        self.assertIn('zero infra attempts recorded', out['infra_attempts_note'])

    def test_15d_fail_report_infra_attempts_malformed(self):
        # STEP 6 case 'malformed': an unreadable infra-attempts.json is
        # reported explicitly; no data is invented.
        tmp = tmp_module(self)
        run = self._run(tmp, {'t_0': 'done', 't_1': 'blocked', 't_2': 'ready'})
        (run / 'reports' / 'failures' / 't_1.json').write_text(json.dumps({'task': 't_1', 'role': 'read-site', 'category': 'TASK_EXECUTION_FAILURE', 'error': 'x', 'at': 'now'}))
        (run / 'control' / 'attempts.json').write_text(json.dumps({'t_1': 2}))
        (run / 'control' / 'infra-attempts.json').write_text('{"t_1": 3, not json')
        out = o.failure_report_command(run.name)
        self.assertEqual(out['state'], 'blocked')
        self.assertIsNone(out['blocked'][0]['infra_attempts'])
        self.assertIn('unreadable', out['infra_attempts_note'])

    def test_25_followup_status_maps_to_current_run(self):
        tmp = tmp_module(self)
        run = self._run(tmp, {'t_0': 'done', 't_1': 'done', 't_2': 'done'})
        (run / 'reports' / 'run-state.json').write_text(json.dumps({
            'state': 'partial_success',
            'field_coverage': {'google_rating': {'resolved_percent': 5.0}},
            'overall_useful_cell_percent': 15.6,
            'iteration_exhausted_tasks': ['find-maps'],
        }))
        o.STATE.write_text(json.dumps({'last_run': str(run), 'runs': [{'run': str(run), 'pid': 999999}]}))
        out = o.latest_command()
        self.assertEqual(out['last_run'], str(run))
        info = o.status_command(run.name)
        self.assertEqual(info['state'], 'complete')
        self.assertEqual(info['run_state'], 'partial_success')
        self.assertEqual(info['coverage']['google_rating']['resolved_percent'], 5.0)
        self.assertEqual(info['iteration_exhausted_tasks'], ['find-maps'])

    def test_26_followup_resume_maps_to_existing_run(self):
        tmp = tmp_module(self)
        run = self._run(tmp, {'t_0': 'done'})
        calls = []
        popen = mock.MagicMock()
        popen.pid = 4244

        def fake_popen(argv, **kw):
            calls.append(list(argv))
            kw['stdout'].close()
            return popen
        with mock.patch.object(o.subprocess, 'Popen', fake_popen):
            out = o.resume_command(run.name)
        self.assertEqual(calls[0], [str(o.LEADGEN), 'run', str(run)])
        self.assertEqual(out['status'], 'resumed')
        spec = json.loads((run / 'control' / 'spec.json').read_text())
        spec['dm_probe'] = True
        (run / 'control' / 'spec.json').write_text(json.dumps(spec))
        with mock.patch.object(o.subprocess, 'Popen', fake_popen):
            with self.assertRaisesRegex(ValueError, 'dm_probe'):
                o.resume_command(run.name)

    def test_27_followup_deliverable_returns_file(self):
        tmp = tmp_module(self)
        run = self._run(tmp, {'t_0': 'done'}, run_name='run-e')
        missing = o.deliverable_command('xlsx', run.name)
        self.assertEqual(missing['status'], 'missing')
        run2 = write_run(tmp, GOOD_SPEC, run_name='run-f', deliverables=('xlsx',))
        out = o.deliverable_command('xlsx', run2.name)
        self.assertEqual(out['status'], 'ok')
        self.assertTrue(out['file'].endswith('leads.xlsx'))


class VerificationTests(unittest.TestCase):
    def _good(self, tmp, run_name='run-test', graph_roles=None):
        return write_run(tmp, GOOD_SPEC, run_name=run_name, rows=full_rows(GOOD_SPEC), deliverables=('md', 'xlsx', 'pdf'), graph_roles=graph_roles)

    def test_16_approved_run_verifies(self):
        tmp = tmp_module(self)
        run = self._good(tmp)
        out = o.verify_command(run.name)
        self.assertTrue(out['verified'], out['errors'])
        self.assertTrue(out['approved'])
        self.assertEqual(out['dm_probe'], False)
        self.assertEqual(out['outreach_events'], [])
        self.assertEqual(out['business_count'], 3)
        self.assertEqual(out['requested_count'], 3)
        self.assertEqual(out['matrix_rows'], 27)
        self.assertEqual(out['expected_rows'], 27)
        self.assertIn('xlsx', out['deliverables'])
        self.assertIn('pdf', out['deliverables'])

    def test_17_approval_hash_mismatch_rejected(self):
        tmp = tmp_module(self)
        run = self._good(tmp)
        approval = json.loads((run / 'control' / 'approval.json').read_text())
        approval['canonical_sha256'] = '0' * 64
        (run / 'control' / 'approval.json').write_text(json.dumps(approval))
        out = o.verify_command(run.name)
        self.assertFalse(out['verified'])
        self.assertIn('canonical SHA256 does not match approval', out['errors'])

    def test_18_wrong_business_count_rejected(self):
        tmp = tmp_module(self)
        run = self._good(tmp)

        def mutate(data):
            data['businesses'] = data['businesses'][:2]
        rewrite_canonical(run, mutate)
        out = o.verify_command(run.name)
        self.assertFalse(out['verified'])
        self.assertTrue(any('business count mismatch' in e for e in out['errors']))

    def test_19_missing_resolution_rows_rejected(self):
        tmp = tmp_module(self)
        run = self._good(tmp)

        def mutate(data):
            data['resolved_fields'] = full_rows(GOOD_SPEC)[:-1]
        rewrite_canonical(run, mutate)
        out = o.verify_command(run.name)
        self.assertFalse(out['verified'])
        self.assertTrue(any('matrix' in e for e in out['errors']))

    def test_20_duplicate_resolution_rows_rejected(self):
        tmp = tmp_module(self)
        run = self._good(tmp)

        def mutate(data):
            data['resolved_fields'] = full_rows(GOOD_SPEC) + [full_rows(GOOD_SPEC)[0]]
        rewrite_canonical(run, mutate)
        out = o.verify_command(run.name)
        self.assertFalse(out['verified'])
        self.assertTrue(any('duplicate resolution rows' in e for e in out['errors']))

    def test_21_error_status_rows_rejected(self):
        tmp = tmp_module(self)
        run = self._good(tmp)

        def mutate(data):
            rows = full_rows(GOOD_SPEC)
            rows[0]['status'] = 'error'
            data['resolved_fields'] = rows
        rewrite_canonical(run, mutate)
        out = o.verify_command(run.name)
        self.assertFalse(out['verified'])
        self.assertTrue(any('error status' in e for e in out['errors']))

    def test_22_missing_deliverable_rejected(self):
        tmp = tmp_module(self)
        run = self._good(tmp)
        (run / 'deliverables' / 'leads.pdf').unlink()
        out = o.verify_command(run.name)
        self.assertFalse(out['verified'])
        self.assertTrue(any('missing deliverable leads.pdf' in e for e in out['errors']))

    def test_23_dm_probe_true_rejected(self):
        tmp = tmp_module(self)
        spec = dict(GOOD_SPEC)
        spec['dm_probe'] = True
        run = write_run(tmp, spec, run_name='run-dm')
        out = o.verify_command(run.name)
        self.assertFalse(out['verified'])
        self.assertTrue(any('dm_probe is not false' in e for e in out['errors']))
        with self.assertRaisesRegex(ValueError, 'dm_probe must be false'):
            o.validate_intent(dict(spec))
        self.assertEqual(out['dm_probe'], True)

    def test_24_outreach_probe_evidence_fails_verification(self):
        tmp = tmp_module(self)
        run = self._good(tmp, run_name='run-p1', graph_roles=['intake', 'probe-send'])
        out = o.verify_command(run.name)
        self.assertFalse(out['verified'])
        self.assertTrue(any('probe/outreach roles' in e for e in out['errors']))
        run2 = self._good(tmp, run_name='run-p2')
        event = json.dumps({'event': 'spawn', 'role': 'probe-send', 'task': 'probe-send-batch-01'})
        (run2 / 'reports' / 'run-metrics.jsonl').write_text(event + '\n')
        out2 = o.verify_command(run2.name)
        self.assertFalse(out2['verified'])
        self.assertEqual(len(out2['outreach_events']), 1)
        self.assertTrue(any('outreach/probe evidence' in e for e in out2['errors']))


class BoundaryAndProfileTests(unittest.TestCase):
    def test_28_orchestrator_has_no_browser_surface(self):
        src = Path(o.__file__).read_text()
        self.assertNotIn('browser_click', src)
        self.assertNotIn('browser_navigate', src)
        self.assertNotIn('import chrome', src)
        profile = Path.home() / '.hermes/profiles/lg-orchestrator/config.yaml'
        if not profile.is_file():
            self.skipTest('local Hermes profile is not part of the release tree')
        cfg = yaml.safe_load(profile.read_text())
        self.assertEqual(cfg['toolsets'], [])
        self.assertEqual(cfg['platform_toolsets'], {'cli': []})
        # Only the narrow leadgen_control MCP bridge may be connected; no
        # browser, messaging or general-purpose MCP servers.
        self.assertEqual(sorted(cfg['mcp_servers']), ['leadgen_control'])
        project_root = Path(o.__file__).resolve().parents[1]
        configured_server = Path(cfg['mcp_servers']['leadgen_control']['args'][0]).resolve()
        if configured_server != (project_root / 'leadgen_control_mcp.py').resolve():
            self.skipTest('local Hermes profile belongs to a different checkout')
        self.assertIn(str(project_root / 'leadgen_control_mcp.py'), json.dumps(cfg['mcp_servers']))
        self.assertEqual(cfg['tools']['tool_search']['enabled'], 'off')
        self.assertIs(cfg['browser']['use_real_profile'], False)

    def test_29_orchestrator_cannot_invoke_probe_roles(self):
        src = Path(o.__file__).read_text()
        self.assertNotIn('launch_worker', src)
        self.assertNotIn('worker_main', src)
        self.assertNotIn('spawn_worker', src)

    def test_30_orchestrator_cannot_write_run_data(self):
        tmp = tmp_module(self)
        run = tmp / 'runs' / 'run-a'
        (run / 'canonical').mkdir(parents=True)
        with self.assertRaises(ValueError):
            o.deliverable_command('xlsx', str(run) + '/canonical/leads.json')
        with self.assertRaises(ValueError):
            o._require_generated_spec(run / 'canonical' / 'leads.json')
        with self.assertRaises(ValueError):
            o.status_command('run-missing')
        r = o.spec_command(SMALL)
        self.assertTrue(str(r['spec_path']).startswith(str(o.SPECS)))

    def test_31_hermes_profile_has_custom_provider(self):
        profile = Path.home() / '.hermes/profiles/lg-orchestrator/config.yaml'
        if not profile.is_file():
            self.skipTest('local Hermes profile is not part of the release tree')
        cfg = yaml.safe_load(profile.read_text())
        self.assertEqual(cfg['providers']['ollama-launch']['api'], 'http://127.0.0.1:11434/v1')
        self.assertEqual(cfg['model']['provider'], 'ollama-launch')
        self.assertIn(cfg['model']['default'], ('gpt-oss:20b-cloud', 'glm-5.3-flash:cloud'))

    def test_32_lg_supervisor_unchanged(self):
        current_path = Path.home() / '.hermes/profiles/lg-supervisor/config.yaml'
        backup_path = Path.home() / '.hermes/backups/ollama-launch-provider-fix-20260922-165629/lg-supervisor/config.yaml'
        if not current_path.is_file() or not backup_path.is_file():
            self.skipTest('local Hermes profile backup is not part of the release tree')
        cur = yaml.safe_load(current_path.read_text())
        backup = yaml.safe_load(backup_path.read_text())
        self.assertEqual(cur['model'], backup['model'])
        self.assertEqual(cur['kanban'], backup['kanban'])
        self.assertEqual(cur.get('toolsets'), backup.get('toolsets'))
        self.assertEqual(cur['browser'], backup['browser'])
        self.assertEqual(cur['providers']['ollama-launch']['api'], 'http://127.0.0.1:11434/v1')


if __name__ == '__main__':
    unittest.main()
