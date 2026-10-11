#!/usr/bin/env python3
"""leadgen_control — narrow MCP stdio bridge between the Hermes desktop UI
(lg-orchestrator profile) and the EXISTING Leadgen Orchestrator control plane.

Exposes exactly five tools — start, status, resume, report, deliverable —
which delegate to the existing app/orchestrator.py command functions. No
arbitrary command execution, no shell, no path arguments, no eval. The only
subprocesses ever spawned are inside the pinned orchestrator module itself
(fixed argv, repository-root cwd, no shell).

Tool arguments are typed and inert: request text stays data forever; run ids
must be bare run names (path traversal is rejected by the orchestrator's own
_resolve_run). dm_probe can never be set: validate_intent refuses any
spec whose dm_probe is not false, and no tool accepts an override.

Run from the repository virtual environment after installing requirements.
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'app'))

import orchestrator as lead_orch  # noqa: E402  (the EXISTING control plane)
import mcp.server.stdio  # noqa: E402
import mcp_types as types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402

RUN_DEFAULT_HINT = ('Bare run name (e.g. run-20260923-071023) or nothing to use the '
                    'orchestrator state last_run.')


def _default_run():
    latest = lead_orch.latest_command()
    if latest.get('status') != 'ok' or not latest.get('last_run'):
        raise ValueError('no run recorded yet; start a run first')
    run = latest['last_run']
    if not isinstance(run, str):
        raise ValueError('state last_run is not a run path')
    return run


def _run_arg(run_id):
    if run_id is None or (isinstance(run_id, str) and not run_id.strip()):
        # An omitted OR empty run_id means "the current (last started) run".
        return _default_run()
    if (not isinstance(run_id, str) or run_id.strip() != run_id
            or '/' in run_id or '\\' in run_id or any(c.isspace() for c in run_id)):
        raise ValueError('run_id must be a bare run directory name')
    return run_id


def _call_text(payload):
    return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(payload, ensure_ascii=False))])


async def _list_tools(ctx, params):
    return types.ListToolsResult(tools=[
        types.Tool(name='start', description=(
            'Normalize a plain-English leadgen research request, write a validated generated spec, and launch the existing deterministic pipeline. Returns run id, count and dm_probe=false. Outreach requests are refused.'),
            inputSchema={'type': 'object', 'properties': {'request_text': {'type': 'string', 'description': 'The user research request VERBATIM as data, e.g. "Find 20 dentists in Ubud. No outreach. Generate Markdown, Excel and PDF."'}}, 'required': ['request_text'], 'additionalProperties': False}),
        types.Tool(name='status', description='Trusted progress for a run (defaults to the last started run).',
            inputSchema={'type': 'object', 'properties': {'run_id': {'type': 'string', 'description': RUN_DEFAULT_HINT}}, 'additionalProperties': False}),
        types.Tool(name='resume', description='Resume a blocked/stopped run through the existing pipeline CLI.',
            inputSchema={'type': 'object', 'properties': {'run_id': {'type': 'string', 'description': RUN_DEFAULT_HINT}}, 'additionalProperties': False}),
        types.Tool(name='report', description='Trusted verification report for a run; adds the hard-block report when the run is blocked.',
            inputSchema={'type': 'object', 'properties': {'run_id': {'type': 'string', 'description': RUN_DEFAULT_HINT}}, 'additionalProperties': False}),
        types.Tool(name='deliverable', description='Locate a finished deliverable inside the run (md, xlsx or pdf only).',
            inputSchema={'type': 'object', 'properties': {'format': {'type': 'string', 'enum': ['md', 'xlsx', 'pdf']}, 'run_id': {'type': 'string', 'description': RUN_DEFAULT_HINT}}, 'additionalProperties': False}),
    ])


def _start(request_text):
    if not isinstance(request_text, str) or not request_text.strip():
        raise ValueError('request_text must be a non-empty string')
    spec = lead_orch.spec_command(request_text)
    if spec.get('status') != 'ok':
        return spec  # 'missing' or 'refused' — relay verbatim, never retry
    launched = lead_orch.launch_command(spec['spec_path'])
    launched['spec_status'] = 'ok'
    launched['summary'] = spec.get('summary')
    launched['dm_probe'] = spec['intent'].get('dm_probe')
    launched['count'] = spec['intent'].get('count')
    return launched


def _report(run_id):
    run = _run_arg(run_id)
    verified = lead_orch.verify_command(run)
    out = {'run': verified.pop('run', run), 'verify': verified}
    if not verified.get('verified'):
        try:
            fr = lead_orch.failure_report_command(run)
            fr.pop('run', None)
            out['failure_report'] = fr
        except Exception as exc:  # noqa: BLE001 — report degrades, never fails
            out['failure_report'] = {'note': 'hard-block report unavailable: %s: %s' % (type(exc).__name__, exc)}
    out['run'] = run
    return out


def _deliverable(fmt, run_id):
    if fmt is None:
        fmt = 'xlsx'
    if not isinstance(fmt, str) or fmt not in lead_orch.FORMATS:
        raise ValueError('format must be one of md, xlsx, pdf')
    return lead_orch.deliverable_command(fmt, _run_arg(run_id))


async def _call_tool(ctx, params):
    args = dict(params.arguments or {})
    try:
        if params.name == 'start':
            return _call_text(_start(args.get('request_text')))
        if params.name == 'status':
            return _call_text(lead_orch.status_command(_run_arg(args.get('run_id'))))
        if params.name == 'resume':
            return _call_text(lead_orch.resume_command(_run_arg(args.get('run_id'))))
        if params.name == 'report':
            return _call_text(_report(args.get('run_id')))
        if params.name == 'deliverable':
            return _call_text(_deliverable(args.get('format'), args.get('run_id')))
        raise ValueError('unknown tool: ' + str(params.name))
    except Exception as exc:  # noqa: BLE001 — the bridge returns errors as tool results, never crashes the session
        return types.CallToolResult(isError=True, content=[types.TextContent(type='text', text='leadgen_control error: %s: %s' % (type(exc).__name__, exc))])


async def main():
    server = Server('leadgen_control', version='1.0.0',
                    instructions='Conversational control plane over the Leadgen pipeline. Only leadgen_control.* tools; no shell.',
                    on_list_tools=_list_tools, on_call_tool=_call_tool)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options(),
                         raise_exceptions=False)


if __name__ == '__main__':
    asyncio.run(main())
