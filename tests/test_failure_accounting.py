from pathlib import Path
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import failure_accounting as fa
import orchestrator as o


class FailureAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run = self.tmp / 'runs' / 'run-fixture'
        for part in ('control', 'reports/failures', 'audit'):
            (self.run / part).mkdir(parents=True, exist_ok=True)
        (self.run / 'run.yaml').write_text('backend: docker\n')
        self.graph = {'bootstrap': {'id': 't_boot', 'role': 'intake', 'parents': []}}
        (self.run / 'control' / 'graph.json').write_text(json.dumps(self.graph))
        db = sqlite3.connect(self.run / 'control' / 'kanban.db')
        db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER, worker_started_at INTEGER, current_run_id INTEGER, last_failure_error TEXT, consecutive_failures INTEGER)')
        db.execute("INSERT INTO tasks VALUES ('t_boot','bootstrap','blocked','lock',1,2,3,4,'startup failed',2)")
        db.commit()
        db.close()

    def _status(self, status='blocked', **extra):
        task = {'id': 't_boot', 'title': 'bootstrap', 'status': status}
        task.update(extra)
        (self.run / 'reports' / 'status.json').write_text(json.dumps({'tasks': [task]}))

    def test_timing_is_primary_and_signatures_are_secondary(self):
        self.assertEqual(fa.classify_failure(first_model_or_tool_call=False, message='bad model output'), 'infrastructure')
        self.assertEqual(fa.classify_failure(first_model_or_tool_call=True, message='permission denied opening worker'), 'infrastructure')
        self.assertEqual(fa.classify_failure(first_model_or_tool_call=True, message='schema invalid'), 'semantic')

    def test_two_startup_failure_fixtures_have_full_contract(self):
        required = {'task', 'class', 'exit', 'stage', 'backend', 'log', 'path', 'last50'}
        for name, message in (('bootstrap', 'update-lock exists'), ('bootstrap-permission', 'permission denied')):
            log = self.run / 'reports' / (name + '.log')
            log.write_text('\n'.join('line %d' % i for i in range(55)) + '\n' + message)
            record = fa.record_failure(
                self.run, task=name,
                failure_class=fa.classify_failure(first_model_or_tool_call=False, message=message),
                exit_code=1, stage='bootstrap', log_path=log, reason=message)
            self.assertTrue(required <= set(record))
            self.assertEqual(record['class'], 'infrastructure')
            self.assertEqual(record['backend'], 'docker')
            self.assertEqual(len(record['last50']), 50)

    def test_infrastructure_charges_only_infra_budget(self):
        fa.charge_attempt(self.run, 't_boot', 'infrastructure')
        self.assertEqual(json.loads((self.run / 'control' / 'infra-attempts.json').read_text()), {'t_boot': 1})
        self.assertFalse((self.run / 'control' / 'attempts.json').exists())

    def test_blocked_status_can_never_have_empty_failures(self):
        self._status(block_reason='worker bootstrap failed before model call', first_model_or_tool_call=False)
        old_runs, old_state = o.RUNS, o.STATE
        self.addCleanup(setattr, o, 'RUNS', old_runs)
        self.addCleanup(setattr, o, 'STATE', old_state)
        o.RUNS = self.tmp / 'runs'
        o.STATE = self.tmp / 'state.json'
        result = o.status_command('run-fixture')
        self.assertEqual(result['state'], 'blocked')
        self.assertTrue(result['failures'])
        self.assertTrue(result['block_reasons'])
        self.assertEqual(result['failures'][0]['type'], 'kanban_block')

    def test_running_tasks_report_running_without_orchestrator_pid(self):
        self._status(status='running')
        old_runs, old_state = o.RUNS, o.STATE
        self.addCleanup(setattr, o, 'RUNS', old_runs)
        self.addCleanup(setattr, o, 'STATE', old_state)
        o.RUNS = self.tmp / 'runs'
        o.STATE = self.tmp / 'state.json'
        self.assertEqual(o.status_command('run-fixture')['state'], 'running')

    def test_reset_infrastructure_task_preserves_log_and_audits(self):
        log = self.run / 'reports' / 'bootstrap.log'
        log.write_text('original startup log')
        fa.record_failure(self.run, task='bootstrap', failure_class='infrastructure', exit_code=1, stage='bootstrap', log_path=log, reason='daemon repaired')
        (self.run / 'control' / 'infra-attempts.json').write_text('{"t_boot": 2}')
        result = fa.reset_task(self.run, 'bootstrap', 'Docker daemon restarted and preflight passed')
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(log.read_text(), 'original startup log')
        self.assertEqual(json.loads((self.run / 'control' / 'infra-attempts.json').read_text()), {})
        db = sqlite3.connect(self.run / 'control' / 'kanban.db')
        self.assertEqual(db.execute("SELECT status FROM tasks WHERE id='t_boot'").fetchone()[0], 'todo')
        db.close()
        failure = json.loads((self.run / 'reports' / 'failures' / 'bootstrap.json').read_text())
        self.assertFalse(failure['active'])
        audit = (self.run / 'audit' / 'supervisor.jsonl').read_text()
        self.assertIn('reset_task', audit)
        self.assertIn('Docker daemon restarted', audit)

    def test_reset_refuses_semantic_and_complete_tasks(self):
        fa.record_failure(self.run, task='bootstrap', failure_class='semantic', exit_code=1, stage='worker')
        with self.assertRaisesRegex(ValueError, 'semantic'):
            fa.reset_task(self.run, 'bootstrap', 'fixed')
        fa.record_failure(self.run, task='bootstrap', failure_class='infrastructure', exit_code=1, stage='bootstrap')
        db = sqlite3.connect(self.run / 'control' / 'kanban.db')
        db.execute("UPDATE tasks SET status='done' WHERE id='t_boot'")
        db.commit()
        db.close()
        with self.assertRaisesRegex(ValueError, 'completed'):
            fa.reset_task(self.run, 'bootstrap', 'fixed')

    def test_reset_requires_repair_evidence(self):
        fa.record_failure(self.run, task='bootstrap', failure_class='infrastructure', exit_code=1, stage='bootstrap')
        with self.assertRaisesRegex(ValueError, 'repair note'):
            fa.reset_task(self.run, 'bootstrap', '')


if __name__ == '__main__':
    unittest.main()
