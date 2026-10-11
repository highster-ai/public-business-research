"""Jev SHADOW collection — passive, non-authoritative, flag-gated.

This module NEVER participates in pipeline decisions. It observes evidence
that the pipeline has already acquired and records an independent Jev
(TypeSafe System One) judgment alongside the existing Hermes result, for
offline calibration only.

Hard guarantees (see tests/test_jev_shadow.py):
- Disabled unless JEV_SHADOW_ENABLED is EXACTLY 'true' in the environment.
- Every failure (missing key, timeout, 4xx/5xx, malformed response, transport
  error) becomes a shadow-log error record only; this module never raises and
  never returns pipeline-visible values. It cannot change task status, trigger
  retries, or alter any pipeline output.
- Writes ONLY under <run>/shadow/jev/ (or an explicit JEV_SHADOW_DIR override
  for isolated replays) — never raw/, resolved/, canonical/, control/,
  deliverables/.
- The API key comes from the TYPESAFE_API_KEY environment variable ONLY
  (never from experiments/jev/.key in this integration); it is sent solely in
  the Authorization header and is never logged, written, or included in
  records (defensively scrubbed from any error text).

Shadow calls are synchronous and bounded (short timeout, one retry for
transient statuses) and run after the stage's own resolution has completed;
correctness of the pipeline always outranks shadow latency.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = 'https://api.typesafe.ai/v1/systemone'
MODEL = 'jev-latest'
TIMEOUT_S = 15.0
MAX_ATTEMPTS = 2          # bounded; only transient statuses are retried
BACKOFF_S = 0.5
MAX_EVIDENCE_CHARS = 3000
RETRYABLE = {429, 500, 502, 503, 504, 529}
SHADOW_ROLES = {'resolve-owner': 'owner', 'resolve-dm': 'review_owner_reply'}

# Centralized shadow question texts (atomic, one decision type each).
QUESTIONS = {
    'owner': {
        'type': 'noul',
        'instructions': {
            'question': 'Does this evidence explicitly identify the named person as an owner, founder, or co-founder of this business?',
            'note': 'Do NOT treat manager, director, CEO, therapist, employee, advisor, '
                    'ambassador, staff, or team member as owner/founder unless ownership '
                    'or founder status is explicitly stated in this evidence. Production '
                    'standard: found only when evidence explicitly supports owner/founder.',
        },
    },
    'review_owner_reply': {
        'type': 'noul',
        'instructions': {
            'question': 'Does this specific review row contain a reply attributable to the business/owner account?',
            'note': 'A reply from a customer, another user, or the platform is NOT a '
                    'business/owner reply. Do NOT evaluate the broader dm_proxy '
                    'definition. dm_probe is false; this is not a measured DM response.',
        },
    },
}


def enabled():
    return os.environ.get('JEV_SHADOW_ENABLED') == 'true'


def sub_enabled(kind):
    # Sub-flags default ON; only an explicit 'false' disables one stream.
    return os.environ.get('JEV_SHADOW_' + kind.upper(), 'true') != 'false'


def _key():
    # Environment ONLY. Never reads experiments/jev/.key. Never logged.
    v = os.environ.get('TYPESAFE_API_KEY')
    return v.strip() if v and v.strip() else None


def _scrub(text, key):
    if key:
        text = text.replace(key, '<redacted>')
    return text


def shadow_dir(run):
    override = os.environ.get('JEV_SHADOW_DIR')
    if override:
        return Path(override).resolve()
    return (Path(run).resolve() / 'shadow' / 'jev')


def _append(path, record, key):
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + '\n'
    with path.open('a') as f:
        f.write(_scrub(line, key))
    path.chmod(0o600)


def _post(state, question, key):
    body = json.dumps({'state': state, 'model': MODEL,
                       'questions': {'q': question}}).encode()
    req = urllib.request.Request(API_URL, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + key,
        'Content-Type': 'application/json',
    })
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                payload = json.load(r)
            answers = payload.get('answers') or {}
            a = answers.get('q') or {}
            value = a.get('noul')
            if not isinstance(value, (int, float)):
                raise ValueError('malformed shadow response: missing noul')
            return {'value': float(value), 'model': payload.get('model'),
                    'input_tokens': (payload.get('usage') or {}).get('input_tokens'),
                    'output_tokens': (payload.get('usage') or {}).get('output_tokens')}
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in RETRYABLE or attempt == MAX_ATTEMPTS:
                raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = exc
            if attempt == MAX_ATTEMPTS:
                raise exc if not isinstance(exc, ValueError) else exc
        time.sleep(BACKOFF_S)
    raise last if last else RuntimeError('shadow call failed')


def _collect(run, kind, business_id, evidence_id, evidence_text, role,
             source_url, existing, business_name=None):
    """One shadow judgment. Returns True if a record was written, else None.
    NEVER raises: every failure path becomes an error record only."""
    key = None
    try:
        if not enabled() or not sub_enabled(kind):
            return None
        key = _key()
        if not key:
            _append(shadow_dir(run) / 'errors.jsonl', {
                'at': _now(), 'run_id': Path(run).name, 'kind': kind,
                'business_id': business_id, 'evidence_id': evidence_id,
                'api_status': 'error', 'error_class': 'MissingAPIKey',
                'error': 'TYPESAFE_API_KEY not present in environment',
            }, None)
            return None
        t0 = time.monotonic()
        result = _post({
            'business_name': business_name,
            'evidence_text': (evidence_text or '')[:MAX_EVIDENCE_CHARS],
            'dm_probe': False,
        }, QUESTIONS[kind], key)
        record = {
            'at': _now(), 'run_id': Path(run).name,
            'business_id': business_id, 'evidence_id': evidence_id,
            'decision_type': kind, 'source_role': role,
            'source_url': source_url,
            'evidence_text': (evidence_text or '')[:MAX_EVIDENCE_CHARS],
            'existing_result': existing,
            'jev_value': result['value'], 'jev_confidence': None,
            'model': result['model'],
            'latency_s': round(time.monotonic() - t0, 3),
            'api_status': 'ok',
            'input_tokens': result['input_tokens'],
            'output_tokens': result['output_tokens'],
        }
        _append(shadow_dir(run) / 'records.jsonl', record, key)
        return True
    except Exception as exc:  # noqa: BLE001 — failure isolation is the contract
        try:
            _append(shadow_dir(run) / 'errors.jsonl', {
                'at': _now(), 'run_id': Path(run).name, 'kind': kind,
                'business_id': business_id, 'evidence_id': evidence_id,
                'api_status': 'error', 'error_class': type(exc).__name__,
                'error': str(exc)[:300],
            }, key)
        except Exception:  # noqa: BLE001 — even error logging must never raise
            pass
        return None


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def collect_stage(run, role, inputs, out):
    """Pipeline hook. Passive: derives NOTHING, modifies NOTHING, raises
    NOTHING. Owner rows are shadowed from resolve-owner inputs; review rows
    (field owner_reply) from resolve-dm inputs. existing_result is the
    pipeline's own resolution for that business, recorded for calibration
    comparison only."""
    try:
        kind = SHADOW_ROLES.get(role)
        if not kind or not enabled():
            return None
        resolutions = (out or {}).get('resolutions') or []
        by_business = {}
        for r in resolutions:
            if r.get('field') == ('owner' if role == 'resolve-owner' else 'dm_proxy'):
                if role == 'resolve-owner':
                    by_business[r['business_id']] = {'status': r.get('status'), 'value': r.get('value')}
                else:
                    comp = ((r.get('components') or {}).get('owner_reply') or {})
                    by_business[r['business_id']] = {'dm_proxy_status': r.get('status'),
                                                     'owner_reply': comp.get('status')}
        for e in (inputs or {}).get('evidence') or []:
            field = e.get('field')
            if role == 'resolve-owner' and field != 'owner':
                continue
            if role == 'resolve-dm' and field != 'owner_reply':
                continue
            _collect(run, kind, e.get('business_id'), e.get('evidence_id'),
                     e.get('snippet') or e.get('raw_text') or '', e.get('reader'),
                     e.get('source_url'), by_business.get(e.get('business_id')))
    except Exception:  # noqa: BLE001 — the hook can never break a pipeline task
        return None