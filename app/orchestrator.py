from core import ROOT, read, write, now, digest, check
from pathlib import Path
import argparse, glob, hashlib, json, os, re, subprocess, sys, time
import yaml
from failure_accounting import ensure_blocked_failure_records, reset_task
from limits import MAX_COUNT, validate_count

# Conversational control plane over the EXISTING deterministic pipeline.
# It never researches, never modifies evidence or canonical data, never
# dispatches workers itself, and never enables outreach: execution is
# delegated verbatim to the pinned `leadgen` CLI with fixed argv arrays,
# and success is only ever confirmed from the run's trusted artifacts.

SPECS = ROOT / 'specs' / 'generated'   # Generated specs live only here.
STATE = ROOT / 'orchestrator' / 'state.json'
LOGS = ROOT / 'orchestrator' / 'logs'
LEADGEN = ROOT / 'leadgen'             # Pinned CLI entry point (existing).
RUNS = ROOT / 'runs'
FIELDS = ['business_name', 'instagram', 'whatsapp', 'public_contact', 'google_rating', 'google_review_count', 'dm_proxy', 'owner']
SOURCES = ['google_maps', 'web', 'instagram']
FORMATS = ['md', 'xlsx', 'pdf']
SOURCE_PHRASES = {'google maps': 'google_maps', 'maps': 'google_maps', 'web': 'web', 'website': 'web', 'websites': 'web', 'official site': 'web', 'instagram': 'instagram'}
FIELD_PHRASES = {'business name': 'business_name', 'instagram': 'instagram', 'whatsapp': 'whatsapp', 'public contact': 'public_contact', 'contact': 'public_contact', 'google rating': 'google_rating', 'review count': 'google_review_count', 'reviews': 'google_review_count', 'owner': 'owner', 'dm proxy': 'dm_proxy', 'dm responsiveness proxy': 'dm_proxy', 'dm measured': 'dm_measured'}
FORMAT_PHRASES = {'markdown': 'md', 'md': 'md', 'excel': 'xlsx', 'xlsx': 'xlsx', 'pdf': 'pdf'}
NUMBER_ONES = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
               'six': 6, 'seven': 7, 'eight': 8, 'nine': 9}
NUMBER_TEENS = {'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13,
                'fourteen': 14, 'fifteen': 15, 'sixteen': 16,
                'seventeen': 17, 'eighteen': 18, 'nineteen': 19}
NUMBER_TENS = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50,
               'sixty': 60}
STAGE_ORDER = ['intake', 'query-plan', 'find-maps', 'find-web', 'dedupe', 'read-google', 'read-site', 'read-ig', 'read-reviews', 'find-owner', 'resolve-ig', 'resolve-wa', 'resolve-owner', 'resolve-dm', 'validator', 'consolidator', 'supervisor', 'render-md', 'render-xlsx', 'render-pdf']
PROBE_ROLES = {'probe-send', 'probe-check'}
OUTREACH_RE = re.compile(r'\b(dm them|message them|contact them|send a dm|send dm|send a whatsapp|whatsapp them|test their dm|reach out)\b|\b(message|dm|whatsapp|contact)\s+(the\s+)?\d*\s*(businesses|dentists|shops|them|owners?|leads)\b')

# ---- deterministic natural-language -> structured intent ------------------

def parse(text):
    '''Normalize a plain-English research request into structured intent.
    Deterministic: model output is never serialized to YAML here. Any
    outreach request is refused at intent level (dm_probe is always False).'''
    flags = {'outreach_requested': False, 'missing': []}
    flat = ' '.join(re.sub(r'[^a-z0-9 ,./-]+', ' ', text.lower()).split())
    m = re.search(r'(?<!\d)(\d+)(?!\d)', flat)
    count = int(m.group(1)) if m else None
    count_span = m.span() if m else None
    if count is None:
        words = {**NUMBER_ONES, **NUMBER_TEENS, **NUMBER_TENS}
        compounds = {
            '%s %s' % (tens, one): tens_value + one_value
            for tens, tens_value in NUMBER_TENS.items() if tens_value < MAX_COUNT
            for one, one_value in NUMBER_ONES.items()
        }
        for phrase, n in sorted({**words, **compounds}.items(), key=lambda item: -len(item[0])):
            wm = re.search(r'\b' + phrase.replace(' ', r'[ -]') + r'\b', flat)
            if wm:
                count = n
                count_span = wm.span()
                break
    if count is not None:
        validate_count(count)
    # Category: the words between the count and the "businesses"/"in " marker.
    category = None
    if count_span is not None:
        seg = flat[count_span[1]:].split('no outreach', 1)[0]
        stop = re.search(r'\b(in|across)\b', seg)
        body = seg[:stop.start()] if stop else seg
        body = re.sub(r'\bbusinesses?\b|\bcompanies?\b', ' ', body)
        body = ' '.join(w for w in body.replace(',', ' ').replace('/', ' ').replace(' and ', ' ').split())
        if body:
            category = body
    # Area: the text after "in " up to a sentence boundary or outreach clause.
    area = None
    am = re.search(r'\b(?:in|across)\s+([^.;]+)', text, re.IGNORECASE)
    if am:
        area = am.group(1)
        for marker in ('no outreach', 'No outreach', 'I need', 'i need', 'Collect', 'collect', 'Give me', 'give me', 'Export', 'export', 'DM responsiveness', 'dm responsiveness', 'DM:', 'DM '):
            area = area.split(marker)[0]
        area = re.split(r'\s+on\s+(?:whatsapp|instagram|google\s+maps|maps)\b', area, flags=re.IGNORECASE)[0]
        area = re.split(r'\s+(?:via|through)\s+', area, flags=re.IGNORECASE)[0]
        area = re.split(r'\s+from\s+(?:google\s+maps|maps|the\s+web|web|website|websites|official\s+site|instagram)\b', area, flags=re.IGNORECASE)[0]
        parts = [p.strip(' .,') for p in re.split(r',|\s+and\s+', area)]
        area = ', '.join(p for p in parts if p)
        area = ' '.join(area.split())
    # Country is never guessed. It may be stated as ``country AE`` or as the
    # final ISO-3166 alpha-2 component of the location (``Dubai Marina, AE``).
    country = None
    country_match = re.search(r'\bcountry\s*[:=]?\s*([a-z]{2})\b', text, re.IGNORECASE)
    if country_match:
        country = country_match.group(1).upper()
        if area:
            area = re.sub(r',?\s*country\s*[:=]?\s*[a-z]{2}\b.*$', '', area,
                          flags=re.IGNORECASE).strip(' ,')
    elif area:
        country_match = re.search(r',\s*([a-z]{2})\s*$', area, re.IGNORECASE)
        if country_match:
            country = country_match.group(1).upper()
            area = area[:country_match.start()].strip(' ,')
    # Explicit source lists are only recognized when phrased as the origin of
    # discovery ("from google maps and instagram"), never when a source word
    # is merely part of a requested field ("I need Instagram"); source-clause
    # words are also excluded from the field scan so "from instagram" is a
    # source, not an instagram-field request. Otherwise the defaults hold.
    clause = None
    cm = re.search(r'\b(?:from|on|across|via|sources?:?)\s+([a-z\s,]+?)(?=[.;]|\bi\b|\bwe\b|\bno\b|\bexport\b|\bgive\b|\bcollect\b|\bneed\b|$)', flat)
    if cm:
        clause = cm.group(1)
    named = []
    for phrase, code in SOURCE_PHRASES.items():
        if clause and re.search(r'\b' + phrase.replace(' ', r'\s+') + r'\b', clause) and code not in named:
            named.append(code)
    sources = sorted(set(named)) if named else None
    spans = [m.span() for m in re.finditer(r'\b(?:from|on|across|via)\s+[a-z\s,]+', flat)] if clause is not None else []
    fields = []
    for phrase, code in FIELD_PHRASES.items():
        m = re.search(r'\b' + phrase.replace(' ', r'\s+') + r'\b', flat)
        if not m or code in fields:
            continue
        if any(s <= m.start() < e for s, e in spans):
            continue
        fields.append(code)
    if fields and 'business_name' not in fields:
        fields = ['business_name'] + fields
    if not fields:
        fields = None
    fmts = []
    for phrase, code in FORMAT_PHRASES.items():
        if re.search(r'\b' + phrase + r'\b', flat) and code not in fmts:
            fmts.append(code)
    formats = sorted(set(fmts), key=FORMATS.index) if fmts else None
    if OUTREACH_RE.search(flat):
        flags['outreach_requested'] = True
    if not count:
        flags['missing'].append('count')
    if not category:
        flags['missing'].append('category')
    if not area:
        flags['missing'].append('area')
    if not country:
        flags['missing'].append('country')
    intent = {
        'category': category,
        'area': area,
        'country': country,
        'count': count,
        'sources': sources if sources else list(SOURCES),
        'requested_fields': fields if fields else list(FIELDS),
        'dm_probe': False,
        'formats': formats if formats else list(FORMATS),
    }
    return {'intent': intent, 'flags': flags}

# ---- intent -> validated, deterministically serialized run spec -----------

def validate_intent(intent):
    '''Schema-validate the structured intent using the EXISTING run schema.
    Also refuses counts beyond verified scale and Instagram-only discovery
    (mirroring the preflight discovery rule); never rewrites user fields.'''
    if not isinstance(intent, dict):
        raise ValueError('Intent must be a JSON object')
    if intent.get('dm_probe') is not False:
        raise ValueError('dm_probe must be false; outreach is refused by the control plane')
    validate_count(intent.get('count'))
    if not isinstance(intent.get('country'), str) or not re.fullmatch(r'[A-Z]{2}', intent['country']):
        raise ValueError('country is required as an ISO-3166 alpha-2 code')
    check('run', intent)
    if 'business_name' not in intent['requested_fields']:
        raise ValueError('requested_fields must include business_name')
    if 'google_maps' not in intent.get('sources', []) and 'web' not in intent.get('sources', []):
        raise ValueError('discovery requires google_maps or web in sources')
    return intent

def _contained(path, parent):
    try:
        p = Path(path).resolve()
        return p if str(p).startswith(str(Path(parent).resolve()) + os.sep) else None
    except Exception:
        return None

def write_spec(intent, request):
    intent = validate_intent(intent)
    SPECS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%d-%H%M%S')
    name = 'leadgen-' + ts
    n = 1
    while (SPECS / (name + '.yaml')).exists():
        n += 1
        name = 'leadgen-' + ts + '-' + str(n)
    spec_path = SPECS / (name + '.yaml')
    spec_path.write_text(yaml.safe_dump(intent, sort_keys=False))
    meta = {'request': request, 'intent': intent, 'spec_path': str(spec_path), 'created_at': now(), 'created_by': 'leadgen-orchestrator', 'runs': []}
    (SPECS / (name + '.meta.json')).write_text(json.dumps(meta, indent=2))
    return spec_path, meta

def summary_text(intent):
    names = {'google_maps': 'Google Maps', 'web': 'Web', 'instagram': 'Instagram', 'md': 'Markdown', 'xlsx': 'Excel', 'pdf': 'PDF'}
    return ('Research request normalized:\n'
        'Category: ' + intent['category'] + '\n'
        'Area: ' + intent['area'] + '\n'
        'Country: ' + intent['country'] + '\n'
        'Businesses: ' + str(intent['count']) + '\n'
        'Sources: ' + ', '.join(names[s] for s in intent['sources']) + '\n'
        'Fields: ' + str(len(intent['requested_fields'])) + '\n'
        'Outputs: ' + ', '.join(names[f] for f in intent['formats']) + '\n'
        'Outreach: Disabled')

def spec_command(text=None, intent_json=None, request=None):
    '''parse -> validate -> deterministic YAML. The LLM may only supply the
    request text, or a structured intent JSON object; the YAML is serialized
    here, never authored by the model.'''
    if intent_json:
        intent = json.loads(intent_json)
        request = request or ''
    else:
        parsed = parse(text)
        if parsed['flags']['outreach_requested']:
            return {'status': 'refused', 'reason': 'Outreach/probing is permanently disabled; the request was not converted into a run.', 'intent': parsed['intent']}
        if parsed['flags']['missing']:
            return {'status': 'missing', 'flags': parsed['flags'], 'intent': parsed['intent']}
        intent = parsed['intent']
        request = text or ''
    spec_path, meta = write_spec(intent, request)
    return {'status': 'ok', 'spec_path': str(spec_path), 'intent': intent, 'summary': summary_text(intent)}

# ---- preflight / launch / resume (fixed argv, no shell) --------------------

def _run_subprocess(argv, timeout=900):
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))

def preflight_command(spec_path):
    p = _run_subprocess([LEADGEN, 'preflight', str(spec_path)])
    try:
        report = json.loads(p.stdout)
    except Exception:
        report = None
    if p.returncode != 0:
        return {'status': 'failed', 'code': p.returncode, 'stderr': p.stderr[-800:], 'stdout': p.stdout[-800:]}
    return {'status': 'ok', 'preflight': report}

def _require_generated_spec(spec):
    if not _contained(spec, SPECS):
        raise ValueError('launch/preflight only accepts generated specs under specs/generated/')
    if not Path(spec).exists():
        raise ValueError('spec not found: ' + str(spec))
    return spec

def launch_command(spec_path):
    '''Delegate execution verbatim to the pinned pipeline CLI and capture the
    machine-readable run id (first stdout line of `leadgen start`).'''
    spec_path = _require_generated_spec(spec_path)
    spec = yaml.safe_load(Path(spec_path).read_text())
    validate_intent(spec)
    pre = preflight_command(spec_path)
    if pre['status'] != 'ok':
        return {'status': 'preflight_failed', 'detail': pre}
    LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / ('launch-' + time.strftime('%Y%m%d-%H%M%S') + '.log')
    with log_path.open('w') as log:
        p = subprocess.Popen([str(LEADGEN), 'start', str(spec_path)], stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True, cwd=str(ROOT))
    deadline = time.monotonic() + 600
    run = None
    while time.monotonic() < deadline:
        if p.poll() is not None:
            break
        lines = [l.strip() for l in log_path.read_text().splitlines() if l.strip()]
        if lines:
            cand = _contained(lines[0], RUNS)
            if cand and cand.is_dir():
                run = cand
                break
        time.sleep(1)
    if run is None:
        return {'status': 'failed', 'reason': 'pipeline runner produced no run directory in time', 'log': str(log_path), 'tail': log_path.read_text()[-800:]}
    log_path.rename(run.parent / (run.name + '.orchestrator.log'))
    state = read(STATE) if STATE.exists() else {'runs': []}
    state['last_run'] = str(run)
    state['last_spec'] = str(spec_path)
    state['updated_at'] = now()
    state['runs'] = [r for r in state.get('runs', []) if r.get('run') != str(run)] + [{'run': str(run), 'spec': str(spec_path), 'pid': p.pid, 'log': str(run.parent / (run.name + '.orchestrator.log')), 'started_at': now()}]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    write(STATE, state)
    meta_path = Path(str(spec_path)[:-5] + '.meta.json')
    if meta_path.exists():
        meta = read(meta_path)
        meta.setdefault('runs', []).append({'run': str(run), 'started_at': now()})
        write(meta_path, meta)
    return {'status': 'launched', 'run': str(run), 'run_id': run.name, 'pid': p.pid, 'spec': str(spec_path)}

def _resolve_run(run):
    if Path(run).is_absolute():
        r = _contained(run, RUNS)
        if r and r.is_dir():
            return r
        raise ValueError('run directory not found under runs/: ' + str(run))
    if '..' in Path(run).parts:
        raise ValueError('run must be a bare run directory name under runs/')
    r = _contained(RUNS / run, RUNS)
    if not r or not r.is_dir():
        raise ValueError('run directory not found: ' + str(run))
    return r

# ---- monitoring from trusted run artifacts only ----------------------------

def _stage_progress(run):
    graph = read(run / 'control' / 'graph.json')
    status = read(run / 'reports' / 'status.json')
    by_key = {t['title']: t['status'] for t in status.get('tasks', [])}
    stages = {}
    for key, node in graph.items():
        role = node['role']
        stages.setdefault(role, {'done': 0, 'total': 0})
        stages[role]['total'] += 1
        if by_key.get(key) == 'done':
            stages[role]['done'] += 1
    order = [r for r in STAGE_ORDER if r in stages] + sorted(set(stages) - set(STAGE_ORDER))
    return graph, by_key, [(role, stages[role]['done'], stages[role]['total']) for role in order]

def _state_entry(run):
    state = read(STATE) if STATE.exists() else {'runs': []}
    return next((r for r in state.get('runs', []) if r.get('run') == str(run)), None)

def status_command(run):
    run = _resolve_run(run)
    graph, by_key, stages = _stage_progress(run)
    status_snapshot = read(run / 'reports' / 'status.json')
    blocked_records = ensure_blocked_failure_records(run, status_snapshot.get('tasks', []), graph)
    failures = [read(f) for f in sorted(glob.glob(str(run / 'reports' / 'failures' / '*.json')))]
    all_done = bool(by_key) and all(s == 'done' for s in by_key.values())
    blocked = [k for k, s in by_key.items() if s in {'blocked', 'triage'}]
    entry = _state_entry(run)
    runner_alive = None
    if entry and entry.get('pid'):
        try:
            os.kill(entry['pid'], 0)
            runner_alive = True
        except Exception:
            runner_alive = False
    tasks_running = any(s == 'running' for s in by_key.values())
    verdict = 'running' if runner_alive or tasks_running else ('complete' if all_done else ('blocked' if blocked else 'idle/stopped'))
    decision_path = run / 'reports' / 'run-state.json'
    decision = read(decision_path) if decision_path.exists() else {}
    run_state = decision.get('state') or ('failed' if verdict == 'blocked' else None)
    block_reasons = [{'task': r['task'], 'reason': r.get('reason'), 'class': r.get('class')} for r in blocked_records]
    return {'run': str(run), 'state': verdict, 'run_state': run_state, 'coverage': decision.get('field_coverage'), 'overall_useful_cell_percent': decision.get('overall_useful_cell_percent'), 'iteration_exhausted_tasks': decision.get('iteration_exhausted_tasks', []), 'source_health': status_snapshot.get('source_health', {}), 'runner_alive': runner_alive, 'progress': [{'stage': s, 'done': d, 'total': t} for s, d, t in stages], 'failures': failures, 'block_reasons': block_reasons}

def failure_report_command(run):
    run = _resolve_run(run)
    info = status_command(run)
    if info['state'] != 'blocked':
        return {'run': str(run), 'note': 'run is not hard-blocked', 'state': info['state']}
    graph = read(run / 'control' / 'graph.json')
    attempts = read(run / 'control' / 'attempts.json') if (run / 'control' / 'attempts.json').exists() else {}
    # Absence of infra-attempts.json means zero recorded infra attempts for
    # this run (the file is only created when an infra failure occurs); it is
    # a valid state, never an error. An unreadable file is reported explicitly
    # instead of being silently treated as data.
    infra = {}
    infra_note = 'no infra-attempts.json present; zero infra attempts recorded'
    infra_path = run / 'control' / 'infra-attempts.json'
    if infra_path.exists():
     try:infra = read(infra_path);infra_note = ''
     except (ValueError, OSError) as exc:infra_note = 'infra-attempts.json present but unreadable: ' + str(exc)[:200]
    rev_parents = {}
    for key, node in graph.items():
        for par in node.get('parents', []):
            rev_parents.setdefault(par, []).append(key)
    hints = {'find-maps': 'Rerun discovery or check Maps reachability via preflight', 'find-web': 'Rerun discovery or widen query plan', 'read-google': 'Re-run the read-google batch after browser health check', 'read-site': 'Re-run the read-site batch after browser health check', 'read-ig': 'Re-run the read-ig batch; login walls are access_restricted, tool failures are error', 'resolve-ig': 'Check resolver output schema; rerun the resolve-ig batch', 'resolve-wa': 'Check whatsapp link evidence; rerun resolve-wa', 'resolve-owner': 'Check public owner evidence; rerun resolve-owner', 'resolve-dm': 'Check dm_proxy derivation from supplied evidence', 'validator': 'Fix upstream resolution errors, never the evidence', 'supervisor': 'Inspect audit/supervisor.jsonl acceptance gate payload', 'render-md': 'Check renderer skill and output directory', 'render-xlsx': 'Check renderer bundled helper', 'render-pdf': 'Check renderer bundled helper'}
    blocks = []
    for t in read(run / 'reports' / 'status.json').get('tasks', []):
        if t['status'] not in {'blocked', 'triage'}:
            continue
        key = t['title']
        node = graph.get(key, {})
        fr = run / 'reports' / 'failures' / (key + '.json')
        rep = read(fr) if fr.exists() else {}
        tid = node.get('id')
        blocks.append({'task': key, 'role': node.get('role'), 'batch': node.get('batch'), 'slice': node.get('slice'), 'failure_category': rep.get('category'), 'attempts': attempts.get(tid), 'infra_attempts': infra.get(tid), 'error': rep.get('error'), 'dependent_tasks': sorted(rev_parents.get(key, [])), 'recommended_next_diagnostic': hints.get(node.get('role'), 'Inspect reports/failures/' + key + '.json and the role log')})
    return {'run': str(run), 'state': 'blocked', 'blocked': blocks, 'infra_attempts_note': infra_note, 'resolution': 'STOP: the control plane will not patch source, alter evidence, or spawn a replacement run; decide whether to repair and resume via `orchestrate resume`.'}

# ---- success verification (trusted artifacts; a "done" run is not enough) --

def verify_command(run):
    run = _resolve_run(run)
    spec = read(run / 'control' / 'spec.json')
    errors = []
    if spec.get('dm_probe') is not False:
        errors.append('spec dm_probe is not false')
    graph = read(run / 'control' / 'graph.json')
    roles = sorted({n['role'] for n in graph.values()})
    probe_roles = sorted(set(roles) & PROBE_ROLES)
    if probe_roles:
        errors.append('probe/outreach roles present in run graph: ' + ','.join(probe_roles))
    approval_path = run / 'control' / 'approval.json'
    approval = read(approval_path) if approval_path.exists() else {}
    data = read(run / 'canonical' / 'leads.json')
    decision_state = approval.get('state', 'approved' if approval.get('approved') is True else None)
    if not approval or decision_state not in {'approved', 'partial_success'}:
        errors.append('supervisor approval missing or not approved; completion decision missing or failed')
    else:
        if digest(data) != approval.get('canonical_sha256'):
            errors.append('canonical SHA256 does not match approval')
        if len(data.get('businesses', [])) != spec['count']:
            errors.append('canonical business count mismatch: %d != %d' % (len(data.get('businesses', [])), spec['count']))
        rows = data.get('resolved_fields', [])
        expected = spec['count'] * len(spec['requested_fields'])
        if len(rows) != expected:
            errors.append('resolution matrix has %d rows, expected %d' % (len(rows), expected))
        pairs = [(r['business_id'], r['field']) for r in rows]
        if len(set(pairs)) != len(pairs):
            errors.append('duplicate resolution rows present')
        if pairs != sorted(set(pairs), key=pairs.index) and len(set(pairs)) == len(pairs):
            pass
        if not data.get('validation', {}).get('valid'):
            errors.append('validation is not valid')
        if any(r['status'] == 'error' for r in rows):
            errors.append('resolution rows with error status present')
    deliverables = {}
    for fmt in spec.get('formats', []):
        out = run / 'deliverables' / ('leads.' + fmt)
        if not out.is_file() or not out.stat().st_size:
            errors.append('missing deliverable leads.' + fmt)
        else:
            deliverables[fmt] = str(out)
    probe_events = []
    metrics = run / 'reports' / 'run-metrics.jsonl'
    if metrics.exists():
        for line in metrics.read_text().splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            if str(ev.get('role', '')).startswith('probe-'):
                probe_events.append({'source': 'run-metrics', 'role': ev.get('role'), 'event': ev.get('event')})
    for f in glob.glob(str(run / 'audit' / '*.jsonl')):
        for line in open(f):
            if '"probe-' in line or re.search(r'"dm_probe"\s*:\s*true', line):
                probe_events.append({'source': os.path.basename(f), 'line': line[:200]})
    if probe_roles or probe_events or spec.get('dm_probe') is not False:
        errors.append('outreach/probe evidence present: ' + json.dumps(probe_events)[:400])
    return {'run': str(run), 'verified': not errors, 'errors': errors, 'state': decision_state, 'dm_probe': spec.get('dm_probe'), 'outreach_events': probe_events, 'approved': approval.get('approved'), 'coverage': approval.get('field_coverage'), 'overall_useful_cell_percent': approval.get('overall_useful_cell_percent'), 'iteration_exhausted_tasks': approval.get('iteration_exhausted_tasks', []), 'canonical_sha256': approval.get('canonical_sha256'), 'business_count': len(data.get('businesses', [])), 'requested_count': spec['count'], 'matrix_rows': len(data.get('resolved_fields', [])), 'expected_rows': spec['count'] * len(spec['requested_fields']), 'validation': data.get('validation'), 'deliverables': deliverables}

def resume_command(run):
    run = _resolve_run(run)
    spec = read(run / 'control' / 'spec.json')
    if spec.get('dm_probe') is not False:
        raise ValueError('refusing to resume a run whose spec has dm_probe enabled')
    log_path = run.parent / (run.name + '.orchestrator.log')
    with log_path.open('a') as log:
        p = subprocess.Popen([str(LEADGEN), 'run', str(run)], stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    state = read(STATE) if STATE.exists() else {'runs': []}
    state['last_run'] = str(run)
    state['updated_at'] = now()
    for r in state.setdefault('runs', []):
        if r.get('run') == str(run):
            r['pid'] = p.pid
            break
    else:
        state['runs'].append({'run': str(run), 'pid': p.pid, 'started_at': now()})
    write(STATE, state)
    return {'status': 'resumed', 'run': str(run), 'pid': p.pid}

def deliverable_command(fmt, run):
    run = _resolve_run(run)
    if fmt not in FORMATS:
        raise ValueError('unknown format ' + fmt)
    out = run / 'deliverables' / ('leads.' + fmt)
    if not out.is_file():
        return {'status': 'missing', 'format': fmt, 'run': str(run)}
    return {'status': 'ok', 'file': str(out), 'sha256': hashlib.sha256(out.read_bytes()).hexdigest(), 'bytes': out.stat().st_size}

def latest_command():
    if not STATE.exists():
        return {'status': 'none'}
    s = read(STATE)
    return {'status': 'ok', 'last_run': s.get('last_run'), 'last_spec': s.get('last_spec'), 'updated_at': s.get('updated_at')}

def main():
    parser = argparse.ArgumentParser(prog='orchestrate')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('parse'); p.add_argument('text')
    p = sub.add_parser('spec'); p.add_argument('text'); p.add_argument('--intent', default=None); p.add_argument('--request', default=None)
    p = sub.add_parser('preflight'); p.add_argument('spec')
    p = sub.add_parser('launch'); p.add_argument('spec')
    p = sub.add_parser('status'); p.add_argument('run')
    p = sub.add_parser('fail'); p.add_argument('run')
    p = sub.add_parser('verify'); p.add_argument('run')
    p = sub.add_parser('resume'); p.add_argument('run')
    p = sub.add_parser('reset-task'); p.add_argument('run'); p.add_argument('task'); p.add_argument('--repair-note', required=True)
    p = sub.add_parser('deliverable'); p.add_argument('fmt'); p.add_argument('run')
    p = sub.add_parser('latest')
    args = parser.parse_args()
    if args.cmd == 'parse':
        out = parse(args.text)
    elif args.cmd == 'spec':
        out = spec_command(args.text, args.intent, args.request)
    elif args.cmd == 'preflight':
        out = preflight_command(_require_generated_spec(args.spec))
    elif args.cmd == 'launch':
        out = launch_command(_require_generated_spec(args.spec))
    elif args.cmd == 'status':
        out = status_command(args.run)
    elif args.cmd == 'fail':
        out = failure_report_command(args.run)
    elif args.cmd == 'verify':
        out = verify_command(args.run)
    elif args.cmd == 'resume':
        out = resume_command(args.run)
    elif args.cmd == 'reset-task':
        out = reset_task(_resolve_run(args.run), args.task, args.repair_note)
    elif args.cmd == 'deliverable':
        out = deliverable_command(args.fmt, args.run)
    elif args.cmd == 'latest':
        out = latest_command()
    print(json.dumps(out, indent=2))

if __name__ == '__main__':
    main()
