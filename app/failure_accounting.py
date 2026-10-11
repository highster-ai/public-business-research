"""Deterministic failure records and retry-budget accounting.

This module deliberately has no Hermes dependency.  Both the pipeline and the
read-only control plane can use it, including while Hermes itself is broken.
"""

from pathlib import Path
import json
import sqlite3

from core import audit, now, read, write


INFRA_SIGNATURES = (
    'bootstrap', 'docker daemon', 'cannot connect to docker', 'image not found',
    'permission denied', 'operation not permitted', 'update lock',
    'update-lock', 'recovery lock', 'address already in use',
)


def classify_failure(*, first_model_or_tool_call=False, message='', timed_out=False):
    """Classify by execution timing first, with log signatures as fallback."""
    text = str(message).lower()
    if not first_model_or_tool_call:
        return 'infrastructure'
    if any(signature in text for signature in INFRA_SIGNATURES):
        return 'infrastructure'
    return 'semantic'


def _tail(path, lines=50):
    path = Path(path) if path else None
    if not path or not path.is_file():
        return []
    return path.read_text(errors='replace').splitlines()[-lines:]


def _backend(run):
    path = Path(run) / 'run.yaml'
    if path.is_file():
        # Avoid making failure reporting depend on YAML being healthy.  Phase 2
        # run metadata writes this as a top-level scalar.
        for line in path.read_text(errors='replace').splitlines():
            if line.startswith('backend:'):
                return line.split(':', 1)[1].strip().strip('"\'')
    return 'unknown'


def record_failure(run, *, task, failure_class, exit_code=None, stage,
                   backend=None, log_path=None, reason=None,
                   failure_type=None, active=True):
    """Write the mandatory, stable failure-record contract for one task."""
    run = Path(run)
    log_path = Path(log_path) if log_path else run / 'reports' / (task + '.log')
    record_path = run / 'reports' / 'failures' / (task + '.json')
    record = {
        'task': task,
        'class': failure_class,
        'exit': exit_code,
        'stage': stage,
        'backend': backend or _backend(run),
        'log': str(log_path),
        'path': str(record_path),
        'last50': _tail(log_path),
        'reason': reason,
        'type': failure_type or ('timeout' if exit_code is None and stage == 'timeout' else 'worker_failure'),
        'active': bool(active),
        'at': now(),
    }
    write(record_path, record)
    return record


def charge_attempt(run, task_id, failure_class):
    """Charge exactly one budget: infra failures never consume semantics."""
    run = Path(run)
    filename = 'infra-attempts.json' if failure_class == 'infrastructure' else 'attempts.json'
    path = run / 'control' / filename
    attempts = read(path) if path.exists() else {}
    attempts[task_id] = attempts.get(task_id, 0) + 1
    write(path, attempts)
    return attempts[task_id]


def ensure_blocked_failure_records(run, tasks, graph=None, backend=None):
    """Make every blocked/triage Kanban card visible as a structured failure."""
    run = Path(run)
    graph = graph or {}
    records = []
    for task in tasks:
        if task.get('status') not in {'blocked', 'triage'}:
            continue
        title = task['title']
        path = run / 'reports' / 'failures' / (title + '.json')
        if path.exists():
            record = read(path)
            # Upgrade legacy worker records without discarding their cause.
            if not {'task', 'class', 'exit', 'stage', 'backend', 'log', 'path', 'last50'} <= set(record):
                legacy = record
                record = record_failure(
                    run, task=title,
                    failure_class=legacy.get('class') or ('infrastructure' if legacy.get('category', '').startswith(('INFRA', 'BROWSER')) else 'semantic'),
                    exit_code=legacy.get('exit'), stage=legacy.get('stage', 'worker'),
                    backend=backend, reason=legacy.get('reason') or legacy.get('error'),
                    failure_type=legacy.get('type', 'worker_failure'))
                # Compatibility keys remain until all writers have migrated to
                # the structured contract; failure reports must not lose cause.
                for key in ('category', 'error', 'at'):
                    if key in legacy:
                        record[key] = legacy[key]
                write(path, record)
        else:
            node = graph.get(title, {})
            reason = task.get('block_reason') or task.get('last_failure_error') or 'Kanban task blocked without a worker failure record'
            record = record_failure(
                run, task=title,
                failure_class=classify_failure(first_model_or_tool_call=task.get('first_model_or_tool_call', True), message=reason),
                exit_code=task.get('exit'), stage=node.get('role', 'kanban'),
                backend=backend, reason=reason, failure_type='kanban_block')
        records.append(record)
    return records


def reset_task(run, task, repair_note):
    """Reset an infrastructure-failed card to the operational pending state."""
    run = Path(run)
    if not str(repair_note or '').strip():
        raise ValueError('reset-task requires a non-empty infrastructure repair note')
    graph = read(run / 'control' / 'graph.json')
    if task not in graph:
        raise ValueError('unknown task: ' + task)
    node = graph[task]
    failure_path = run / 'reports' / 'failures' / (task + '.json')
    if not failure_path.exists():
        raise ValueError('reset-task requires a recorded infrastructure failure')
    failure = read(failure_path)
    if failure.get('class') != 'infrastructure':
        raise ValueError('reset-task refuses semantic failures')
    db_path = run / 'control' / 'kanban.db'
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute('SELECT status FROM tasks WHERE id=?', (node['id'],)).fetchone()
        if not row:
            raise ValueError('task is absent from Kanban: ' + task)
        if row[0] == 'done':
            raise ValueError('reset-task refuses completed tasks')
        if row[0] not in {'blocked', 'triage'}:
            raise ValueError('reset-task only accepts blocked or triage tasks')
        # Hermes calls its pending state ``todo``.
        conn.execute(
            "UPDATE tasks SET status='todo', claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL, worker_started_at=NULL, current_run_id=NULL, "
            "last_failure_error=NULL, consecutive_failures=0 WHERE id=?",
            (node['id'],))
        conn.commit()
    finally:
        conn.close()
    infra_path = run / 'control' / 'infra-attempts.json'
    infra = read(infra_path) if infra_path.exists() else {}
    previous = infra.pop(node['id'], 0)
    write(infra_path, infra)
    failure['active'] = False
    failure['reset_at'] = now()
    failure['repair_note'] = str(repair_note).strip()
    write(failure_path, failure)
    audit(run, 'supervisor', 'reset_task', {
        'task': task, 'task_id': node['id'], 'from': row[0], 'to': 'todo',
        'display_state': 'pending', 'cleared_infra_attempts': previous,
        'repair_note': str(repair_note).strip(), 'logs_preserved': True,
    })
    return {'status': 'reset', 'task': task, 'task_id': node['id'], 'state': 'pending', 'cleared_infra_attempts': previous}
