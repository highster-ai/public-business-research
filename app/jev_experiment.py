"""Jev LIVE A/B experiment hooks (mandate M1+M2) — env-gated, default OFF,
fail closed to the unmodified Hermes path.

Activation requires JEV_AB_ENABLED to be EXACTLY 'true' in the environment.
With the flag unset (or any other value) every entry point below returns None
immediately and the production path is byte-identical to the pre-experiment
route (asserted by tests/test_jev_experiment_hooks.py).

Hard guarantees:
- Jev never browses: no browser import here; the Maps hook only feeds Jev text
  the production browser already captured, and any navigation is performed by
  the production find-maps browser role on the experimental path only.
- Jev never performs outreach and never sets dm_probe (always False in state).
- Jev never mints evidence, source URLs, business identities, owner names or
  canonical values. Owner screening only produces status 'not_found' rows via
  the existing deterministic resolution()/validate_resolution() helpers,
  citing solely that business's own preserved evidence ids. A Jev-confident
  NEGATIVE may skip the Hermes resolve-owner call (mandate S8); every
  possible-positive/uncertain/error case goes to the UNMODIFIED Hermes path,
  so a Jev positive can never become final without Hermes verification.
- The Maps hook may only pre-select among candidate listings that ALREADY
  passed the deterministic name matcher, and only on the path where the
  deterministic selector found no unique match (otherwise it is a no-op);
  the selected listing still passes every existing identity check
  (names_match heading on the place page, location corroboration). Jev is
  never the final identity authority.
- Missing key, API error, malformed response, missing evidence: fail closed
  to Hermes.
- Records are written ONLY under <run>/shadow/jev_ab/ (or JEV_AB_DIR override),
  mode 0600. The TYPESAFE_API_KEY comes from the environment ONLY and is
  never logged or written (defensively scrubbed from records).
"""
import os
import time

QUESTIONS = {
    # M1 (S5+S8): one atomic question per business over its own preserved
    # owner evidence. Confident NO may skip the Hermes resolve-owner call.
    'owner_evidence_screen': {
        'type': 'noul',
        'instructions': {
            'question': 'Does ANY of this evidence explicitly identify a specific person as the owner, founder, or co-founder of this business?',
            'note': 'Judge ONLY the given evidence text; no outside knowledge. A manager, director, CEO, therapist, employee, staff member, advisor, consultant or ambassador does NOT qualify unless ownership or founder status is also explicitly stated. A company/PT/legal-entity owner without a named person does NOT qualify. Production standard: owner is found only from an explicit owner/founder statement naming a person.',
        },
    },
    # M2 (S7): one atomic question per candidate Maps listing line.
    'maps_identity': {
        'type': 'noul',
        'instructions': {
            'question': 'Does this Maps listing represent this exact business (same business, any branch/location variant)?',
            'note': 'Judge ONLY the given listing text and business identity; no outside knowledge. An advertisement, a different business, or a site link for another business is NOT the same business.',
        },
    },
}


def enabled():
    return os.environ.get('JEV_AB_ENABLED') == 'true'


def owner_screening_enabled():
    return enabled() and os.environ.get('JEV_AB_OWNER', 'true') != 'false'


def maps_triage_enabled():
    return enabled() and os.environ.get('JEV_AB_MAPS', 'true') != 'false'


def owner_threshold():
    try:
        return float(os.environ.get('JEV_AB_OWNER_T', '0.90'))
    except ValueError:
        return 0.90


def maps_threshold():
    try:
        return float(os.environ.get('JEV_AB_MAPS_T', '0.60'))
    except ValueError:
        return 0.60


def _key():
    # Environment ONLY. Never reads any key file. Never logged.
    v = os.environ.get('TYPESAFE_API_KEY')
    return v.strip() if v and v.strip() else None


def shadow_dir(run):
    override = os.environ.get('JEV_AB_DIR')
    if override:
        import pathlib
        return pathlib.Path(override).resolve()
    import pathlib
    return pathlib.Path(run).resolve() / 'shadow' / 'jev_ab'


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(run, record):
    # Failure-isolated experiment log. Never raises; never contains the key.
    try:
        import jev_shadow
        jev_shadow._append(shadow_dir(run) / 'records.jsonl', record, _key())
    except Exception:  # noqa: BLE001 — the experiment log can never break a task
        pass


def _error(run, record):
    try:
        import jev_shadow
        jev_shadow._append(shadow_dir(run) / 'errors.jsonl', record, _key())
    except Exception:  # noqa: BLE001
        pass


def _ask(business_name, evidence_text, kind):
    # One transport, shared with the production shadow module (same endpoint,
    # same bounded retry semantics). Returns the noul probability or None.
    import jev_shadow
    key = _key()
    if not key:
        return None
    state = {'business_name': business_name,
             'evidence_text': (evidence_text or '')[:3000],
             'dm_probe': False}
    result = jev_shadow._post(state, QUESTIONS[kind], key)
    return float(result['value'])


# ---------------------------------------------------------------------------
# M1 — resolve-owner screening (S8 + S5). Call site: worker.resolve().
# ---------------------------------------------------------------------------

def owner_screen(run, inputs, spec, resolve_model_fn):
    """Returns {'resolutions': [...]} if the experimental screening took over
    (at least one business screened out), else None (full Hermes path).
    Never raises; every failure mode falls back to wholesale Hermes."""
    try:
        if not owner_screening_enabled():
            return None
        key = _key()
        if not key:
            _error(run, {'at': _now(), 'kind': 'owner_evidence_screen',
                         'error_class': 'MissingAPIKey',
                         'error': 'TYPESAFE_API_KEY not present in environment'})
            return None
        businesses = inputs.get('businesses') or []
        ev = [e for e in inputs.get('evidence') or [] if e.get('field') == 'owner']
        if not ev:
            return None  # nothing to screen; Hermes path exactly as today
        by_biz = {}
        for e in ev:
            by_biz.setdefault(e.get('business_id'), []).append(e)
        screened, to_hermes = [], []
        t = owner_threshold()
        for b in businesses:
            rows = by_biz.get(b.get('business_id')) or []
            if not rows:
                to_hermes.append(b)  # mandate 9/test: missing evidence -> Hermes
                continue
            text = '\n---\n'.join(
                f"[{r.get('source_url','')}] {r.get('snippet') or r.get('raw_text','')}"
                for r in rows)
            t0 = time.monotonic()
            try:
                p = _ask(b.get('name_raw', ''), text, 'owner_evidence_screen')
            except Exception as exc:  # noqa: BLE001 — API failure -> Hermes
                _error(run, {'at': _now(), 'kind': 'owner_evidence_screen',
                             'business_id': b.get('business_id'),
                             'error_class': type(exc).__name__,
                             'error': str(exc)[:300]})
                to_hermes.append(b)
                continue
            _record(run, {'at': _now(), 'kind': 'owner_evidence_screen',
                          'business_id': b.get('business_id'),
                          'evidence_ids': [r.get('evidence_id') for r in rows],
                          'p_owner_evidence_present': p, 'threshold': t,
                          'decision': 'screened_no_owner' if p <= 1 - t else 'to_hermes',
                          'latency_s': round(time.monotonic() - t0, 3),
                          'dm_probe': False})
            if p <= 1 - t:  # confident NO_OWNER_EVIDENCE -> may skip Hermes
                screened.append((b, rows, p))
            else:
                to_hermes.append(b)  # positive/possible/uncertain -> Hermes mandatory
        if not screened:
            return None  # nothing screened out; keep the production batch intact
        from core import resolution, validate_resolution
        out = []
        for b, rows, p in screened:
            r = resolution(b['business_id'], 'owner', 'not_found', None,
                           [e['evidence_id'] for e in rows], ev,
                           f'Jev live A/B screening: preserved evidence judged to contain no explicit owner/founder statement '
                           f'(p_owner_evidence_present={p:.3f}, threshold {t}); Hermes resolve-owner call avoided; '
                           f'no value minted; evidence retained', 0)
            validate_resolution(r, ev, spec['country'])
            out.append(r)
        if to_hermes:
            sub = dict(inputs)
            sub['businesses'] = to_hermes
            hm = resolve_model_fn(run, 'resolve-owner', sub, spec)
            out += hm.get('resolutions') or []
        return {'resolutions': out}
    except Exception:  # noqa: BLE001 — any screening failure falls back wholesale
        return None


# ---------------------------------------------------------------------------
# M2 — Maps identity triage (S7). Call site: worker.reader(), find-maps
# targets branch, ONLY on the deterministic-selector failure path, plus a
# post-selection verification annotation on every successful listing.
# ---------------------------------------------------------------------------

def maps_rescue(run, browser, b, spec, exc):
    """Called when quality.maps_listing raised. Returns a Maps page dict ONLY
    when Jev confidently and uniquely pre-selects one of the candidate
    listings that already passed the deterministic name matcher; the selected
    listing is then re-verified by the UNMODIFIED maps_listing checks.
    Returns None to reproduce the original error (deterministic behavior)."""
    try:
        if not maps_triage_enabled():
            return None
        if 'not uniquely identified' not in str(exc):
            return None
        key = _key()
        if not key:
            _error(run, {'at': _now(), 'kind': 'maps_identity',
                         'error_class': 'MissingAPIKey',
                         'error': 'TYPESAFE_API_KEY not present in environment'})
            return None
        from urllib.parse import quote
        from quality import page, links, target, names_match, domain
        start = b.get('url', '')
        if not (domain(start) in {'google.com', 'maps.google.com'} and '/maps/place/' in start):
            start = 'https://www.google.com/maps/search/' + quote(b['name_raw'] + ' ' + spec['area']) + '?hl=en'
        s = page(browser, start)
        # Candidates: the SAME filter maps_listing itself applies. Jev never
        # sees a line the deterministic name matcher rejected.
        cands = [(name, ref) for name, ref in links(s)
                 if names_match(b['name_raw'], name)
                 and not __import__('re').search('website|directions|book', name, __import__('re').I)][:8]
        if len(cands) < 2:
            return None
        t = maps_threshold()
        ps, t0 = [], time.monotonic()
        for name, ref in cands:
            line = next((ln for ln in s['untrusted_page_text'].splitlines()
                         if f'link "{name}"' in ln), '')
            try:
                p = _ask(b.get('name_raw', ''),
                         f"Candidate listing: {name}\nLine: {line}\nArea: {spec['area']}",
                         'maps_identity')
            except Exception as e2:  # noqa: BLE001 — API failure -> deterministic error
                _error(run, {'at': _now(), 'kind': 'maps_identity',
                             'business_id': b.get('business_id'),
                             'error_class': type(e2).__name__, 'error': str(e2)[:300]})
                return None
            ps.append(p)
        _record(run, {'at': _now(), 'kind': 'maps_identity',
                      'business_id': b.get('business_id'),
                      'candidates': [c[0] for c in cands],
                      'p_values': ps, 'threshold': t,
                      'decision': 'pre_selected' if sum(1 for p in ps if p >= t) == 1 else 'fallback_deterministic',
                      'latency_s': round(time.monotonic() - t0, 3),
                      'dm_probe': False})
        confident = [(cand, p) for cand, p in zip(cands, ps) if p >= t]
        if len(confident) != 1:
            return None  # uncertain/multiple -> deterministic behavior unchanged
        (_, ref), _ = confident[0]
        _, _, url = target(browser, ref)
        if '/maps/place/' not in url:
            return None
        # All existing maps_listing identity checks now run against the
        # pre-selected listing; Jev is never the final identity authority.
        from quality import maps_listing
        return maps_listing(browser, dict(b, url=url), spec)
    except Exception:  # noqa: BLE001 — never mask the original deterministic error
        return None


def maps_verify(run, s, b, spec):
    """Post-selection verification annotation on an ALREADY-accepted listing.
    Pure observation: records whether Jev agrees with the deterministic
    identity selection. Never raises, never affects routing."""
    try:
        if not maps_triage_enabled():
            return
        key = _key()
        if not key:
            return
        from quality import headings, text
        hs = [h for h in headings(s) if b.get('name_raw', '').casefold() in h.casefold()]
        name = hs[0] if hs else (headings(s) or [''])[0]
        t0 = time.monotonic()
        try:
            p = _ask(b.get('name_raw', ''),
                     f"Maps listing heading: {name}\nURL: {s.get('url','')}\n"
                     f"Lines: {chr(10).join(text(s).splitlines()[:12])}\nArea: {spec['area']}",
                     'maps_identity')
        except Exception as exc:  # noqa: BLE001 — annotation only
            _error(run, {'at': _now(), 'kind': 'maps_verify',
                         'business_id': b.get('business_id'),
                         'error_class': type(exc).__name__, 'error': str(exc)[:300]})
            return
        _record(run, {'at': _now(), 'kind': 'maps_verify',
                      'business_id': b.get('business_id'),
                      'listing_name': name, 'p_same_business': p,
                      'agrees_with_deterministic_selection': p >= maps_threshold(),
                      'latency_s': round(time.monotonic() - t0, 3),
                      'dm_probe': False})
    except Exception:  # noqa: BLE001 — annotation can never break a task
        pass