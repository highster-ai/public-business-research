# Tests for the leadgen_control MCP bridge (Hermes desktop UI → orchestrator).
# The server module is exercised through the REAL mcp SDK stdio client (the
# same client path Hermes uses) plus direct source-level security assertions.
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT))

PROFILE = Path.home() / '.hermes/profiles/lg-orchestrator'
SERVER = ROOT / 'leadgen_control_mcp.py'

import orchestrator as lead_orch  # noqa: E402
from limits import COUNT_CAP_ERROR  # noqa: E402


def _tc(name, arguments):
    params = mock.Mock()
    params.name = name
    params.arguments = arguments
    return params


def _load_server_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('leadgen_control_mcp', SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(coro):
    return asyncio.run(coro)


class BridgeSurface(unittest.TestCase):
    """1. Hermes profile can discover leadgen_control (via the real MCP client)."""

    def test_mcp_client_discovers_five_tools(self):
        from mcp import ClientSession, StdioServerParameters as SP
        from mcp.client.stdio import stdio_client
        async def go():
            async with stdio_client(SP(command=sys.executable, args=[str(SERVER)])) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    return sorted(t.name for t in (await s.list_tools()).tools)
        self.assertEqual(run(go()), ['deliverable', 'report', 'resume', 'start', 'status'])

    def test_profile_config_registers_server(self):
        if not (PROFILE / 'config.yaml').is_file():
            self.skipTest('local Hermes profile is not part of the release tree')
        import yaml
        cfg = yaml.safe_load((PROFILE / 'config.yaml').read_text())
        entry = (cfg.get('mcp_servers') or {}).get('leadgen_control')
        self.assertIsInstance(entry, dict)
        self.assertTrue(str(entry['command']).endswith('/bin/python'))
        self.assertTrue(str(entry['args'][0]).endswith('/leadgen_control_mcp.py'))

    def test_tool_schemas_are_narrow(self):
        mod = _load_server_module()
        tools = run(mod._list_tools(None, None)).tools
        by_name = {t.name: t for t in tools}
        self.assertEqual(set(by_name), {'start', 'status', 'resume', 'report', 'deliverable'})
        start = by_name['start'].input_schema
        self.assertEqual(start['properties'], {'request_text': {'type': 'string', 'description': mock.ANY}})
        self.assertEqual(start['required'], ['request_text'])
        for name in ('status', 'resume', 'report'):
            self.assertEqual(set(by_name[name].input_schema['properties']), {'run_id'})
        self.assertEqual(set(by_name['deliverable'].input_schema['properties']), {'format', 'run_id'})
        self.assertEqual(by_name['deliverable'].input_schema['properties']['format']['enum'], ['md', 'xlsx', 'pdf'])
        for t in tools:
            self.assertEqual(t.input_schema.get('additionalProperties'), False)


class NoShellNoArbitraryExec(unittest.TestCase):
    """2. Generic shell is unavailable. 3. Arbitrary commands cannot be supplied."""

    def test_no_shell_no_subprocess_no_eval_in_server(self):
        src = SERVER.read_text()
        self.assertNotIn('shell=True', src)
        self.assertNotIn('os.system', src)
        self.assertNotIn('eval(', src)
        self.assertNotIn('exec(', src)
        self.assertNotIn('os.system', src)
        self.assertNotIn('shlex', src)
        import ast
        tree = ast.parse(src)
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported.update(a.name.split('.')[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                imported.add((n.module or '').split('.')[0])
        self.assertFalse(imported & {'subprocess', 'socket', 'http', 'smtplib'}, imported)

    def test_profile_has_no_cli_or_generic_toolsets(self):
        if not (PROFILE / 'config.yaml').is_file():
            self.skipTest('local Hermes profile is not part of the release tree')
        import yaml
        cfg = yaml.safe_load((PROFILE / 'config.yaml').read_text())
        self.assertEqual(cfg.get('toolsets'), [])
        self.assertEqual(cfg.get('platform_toolsets', {}).get('cli'), [])
        # only the one MCP server is connected
        self.assertEqual(list(cfg.get('mcp_servers')), ['leadgen_control'])

    def test_only_five_tool_names_routed(self):
        mod = _load_server_module()
        async def go():
            r = await mod._call_tool(None, _tc('rm_rf', {}))
            return r
        r = run(go())
        self.assertTrue(r.is_error)
        self.assertIn('unknown tool', r.content[0].text)

    def test_supervisor_profile_unchanged(self):
        # 13. Lg Supervisor profile untouched by this work.
        cfg = Path.home() / '.hermes/profiles/lg-supervisor/config.yaml'
        self.assertIn('mcp_servers: {}', cfg.read_text()) if cfg.exists() else self.skipTest('no supervisor config')


class StartPath(unittest.TestCase):
    """4. start invokes the existing orchestrator path. 5. Request text stays data.
    6. start returns a real run id. 12. dm_probe=true impossible."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, True))
        self._patches = [
            mock.patch.object(lead_orch, 'SPECS', self.tmp / 'specs' / 'generated'),
            mock.patch.object(lead_orch, 'STATE', self.tmp / 'orchestrator' / 'state.json'),
            mock.patch.object(lead_orch, 'LOGS', self.tmp / 'orchestrator' / 'logs'),
            mock.patch.object(lead_orch, 'RUNS', self.tmp / 'runs'),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        (self.tmp / 'specs' / 'generated').mkdir(parents=True)

    def test_start_refuses_outreach_and_relay_verbatim(self):
        mod = _load_server_module()
        r = json.loads(run(mod._call_tool(None, _tc('start', {'request_text': 'Message the 20 dentists in Ubud on WhatsApp.'}))).content[0].text)
        self.assertEqual(r['status'], 'refused')

    def test_start_missing_params_relayed(self):
        mod = _load_server_module()
        r = json.loads(run(mod._call_tool(None, _tc('start', {'request_text': 'Find bookshops.'}))).content[0].text)
        self.assertEqual(r['status'], 'missing')
        self.assertIn('count', r['flags']['missing'])

    def test_start_launches_via_existing_launch_command(self):
        mod = _load_server_module()
        with mock.patch.object(lead_orch, 'launch_command', return_value={'status': 'launched', 'run': '/tmp/runs/run-x', 'run_id': 'run-x'}) as lc:
            r = json.loads(run(mod._call_tool(None, _tc('start', {'request_text': 'Find 3 bookshops in Ubud, ID. No outreach. Generate Markdown, Excel and PDF.'}))).content[0].text)
        self.assertEqual(r['run_id'], 'run-x')
        self.assertEqual(r['dm_probe'], False)
        self.assertEqual(r['count'], 3)
        spec_path = Path(json.loads(json.dumps(lc.call_args[0][0])))
        self.assertTrue(str(spec_path).startswith(str(self.tmp / 'specs' / 'generated')))
        spec = json.loads(json.dumps(__import__('yaml').safe_load(Path(spec_path).read_text())))
        self.assertEqual(spec['dm_probe'], False)
        # the launch delegate receives the generated spec path, nothing else
        self.assertTrue(str(lc.call_args[0][0]).endswith('.yaml'))

    def test_count_boundary_60_accepted_and_61_rejected_with_canonical_message(self):
        mod = _load_server_module()
        with mock.patch.object(lead_orch, 'launch_command', return_value={'status': 'launched', 'run_id': 'run-60'}):
            accepted = run(mod._call_tool(None, _tc('start', {
                'request_text': 'Find 60 spas in Dubai Marina, AE. No outreach.'
            })))
        self.assertFalse(accepted.is_error)
        self.assertEqual(json.loads(accepted.content[0].text)['count'], 60)
        rejected = run(mod._call_tool(None, _tc('start', {
            'request_text': 'Find 61 spas in Dubai Marina, AE. No outreach.'
        })))
        self.assertTrue(rejected.is_error)
        self.assertIn(COUNT_CAP_ERROR, rejected.content[0].text)

    def test_request_text_never_becomes_command_syntax(self):
        # 5. A malicious request string is stored verbatim as data (meta), and
        # the spec it produces is schema-checked data, never executed.
        mod = _load_server_module()
        evil = 'Find 3 bookshops in Ubud, ID; rm -rf / && cat ~/.hermes/config.yaml. No outreach.'
        with mock.patch.object(lead_orch, 'launch_command', return_value={'status': 'launched', 'run_id': 'run-x'}) as lc:
            json.loads(run(mod._call_tool(None, _tc('start', {'request_text': evil}))).content[0].text)
        meta = json.loads(Path(str(lc.call_args[0][0])[:-5] + '.meta.json').read_text())
        self.assertEqual(meta['request'], evil)  # inert data, preserved verbatim
        import yaml as _y
        spec = _y.safe_load(Path(lc.call_args[0][0]).read_text())
        self.assertEqual(spec['count'], 3)

    def test_dm_probe_true_impossible(self):
        # 12. No tool accepts a dm_probe argument; validate_intent refuses any
        # intent whose dm_probe is not false, so the bridge cannot enable it.
        mod = _load_server_module()
        tools = run(mod._list_tools(None, None)).tools
        for t in tools:
            self.assertNotIn('dm_probe', json.dumps(t.input_schema))
        with self.assertRaisesRegex(ValueError, 'dm_probe must be false'):
            lead_orch.validate_intent({'category': 'bookshops', 'area': 'Ubud', 'country': 'ID', 'count': 3,
                                       'sources': ['web'], 'requested_fields': ['business_name'], 'dm_probe': True, 'formats': ['md']})


class FollowUps(unittest.TestCase):
    """7-11. status/resume/report/deliverable + path traversal."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, True))
        for p in [mock.patch.object(lead_orch, 'STATE', self.tmp / 'state.json'),
                  mock.patch.object(lead_orch, 'RUNS', self.tmp / 'runs')]:
            p.start()
            self.addCleanup(p.stop)
        (self.tmp / 'runs' / 'run-a').mkdir(parents=True)

    def test_run_arg_default_and_traversal(self):
        mod = _load_server_module()
        with mock.patch.object(mod.lead_orch, 'latest_command', return_value={'status': 'ok', 'last_run': '/x/run-a'}):
            self.assertEqual(mod._run_arg(None), '/x/run-a')
        with mock.patch.object(mod.lead_orch, 'latest_command', return_value={'status': 'none'}):
            with self.assertRaisesRegex(ValueError, 'no run recorded'):
                mod._run_arg(None)
        for bad in ('../../etc', 'run-a/x', ' run-a', 'run\na'):
            with self.assertRaises(ValueError):
                mod._run_arg(bad)
        # an explicitly EMPTY run_id (LLMs emit {"run_id": ""}) = current run
        with mock.patch.object(mod.lead_orch, 'latest_command', return_value={'status': 'ok', 'last_run': '/x/run-b'}):
            self.assertEqual(mod._run_arg(''), '/x/run-b')

    def test_status_reads_requested_run(self):
        mod = _load_server_module()
        graph = {'intake-1': {'role': 'intake', 'status': 'done'}}
        (self.tmp / 'runs' / 'run-a' / 'control').mkdir()
        (self.tmp / 'runs' / 'run-a' / 'control' / 'graph.json').write_text(json.dumps(graph))
        (self.tmp / 'runs' / 'run-a' / 'reports').mkdir()
        (self.tmp / 'runs' / 'run-a' / 'reports' / 'status.json').write_text(json.dumps({'tasks': [{'title': 'intake-1', 'status': 'done'}]}))
        r = json.loads(run(mod._call_tool(None, _tc('status', {'run_id': 'run-a'}))).content[0].text)
        self.assertEqual(r['run'], str(self.tmp / 'runs' / 'run-a'))
        self.assertEqual(r['progress'][0]['stage'], 'intake')

    def test_resume_targets_requested_run(self):
        mod = _load_server_module()
        spec = {'dm_probe': False, 'count': 3}
        (self.tmp / 'runs' / 'run-a' / 'control').mkdir(parents=True)
        (self.tmp / 'runs' / 'run-a' / 'control' / 'spec.json').write_text(json.dumps(spec))
        with mock.patch.object(lead_orch, 'LEADGEN', self.tmp / 'leadgen-fake'), \
             mock.patch('subprocess.Popen') as po:
            po.return_value.pid = 4242
            r = json.loads(run(mod._call_tool(None, _tc('resume', {'run_id': 'run-a'}))).content[0].text)
            po.call_args.kwargs['stdout'].close()
        self.assertEqual(r['status'], 'resumed')
        self.assertIn('run-a', r['run'])
        argv = po.call_args[0][0]
        self.assertEqual(argv, [str(self.tmp / 'leadgen-fake'), 'run', str(self.tmp / 'runs' / 'run-a')])

    def test_report_reads_trusted_report(self):
        mod = _load_server_module()
        (self.tmp / 'runs' / 'run-a' / 'control').mkdir(parents=True)
        (self.tmp / 'runs' / 'run-a' / 'control' / 'spec.json').write_text(json.dumps({'dm_probe': False, 'count': 1, 'requested_fields': ['business_name'], 'formats': ['md']}))
        (self.tmp / 'runs' / 'run-a' / 'control' / 'graph.json').write_text(json.dumps({}))
        (self.tmp / 'runs' / 'run-a' / 'control' / 'approval.json').write_text(json.dumps({}))
        (self.tmp / 'runs' / 'run-a' / 'canonical').mkdir()
        (self.tmp / 'runs' / 'run-a' / 'canonical' / 'leads.json').write_text(json.dumps({'businesses': [], 'resolved_fields': []}))
        r = json.loads(run(mod._call_tool(None, _tc('report', {'run_id': 'run-a'}))).content[0].text)
        self.assertFalse(r['verify']['verified'])
        self.assertIn('supervisor approval missing or not approved', ''.join(r['verify']['errors']))

    def test_deliverable_only_md_xlsx_pdf_contained(self):
        mod = _load_server_module()
        (self.tmp / 'runs' / 'run-a' / 'deliverables').mkdir(parents=True)
        (self.tmp / 'runs' / 'run-a' / 'deliverables' / 'leads.xlsx').write_bytes(b'x')
        r = json.loads(run(mod._call_tool(None, _tc('deliverable', {'format': 'xlsx', 'run_id': 'run-a'}))).content[0].text)
        self.assertEqual(r['status'], 'ok')
        self.assertIn(str(self.tmp / 'runs' / 'run-a' / 'deliverables' / 'leads.xlsx'), r['file'])
        for bad in ('json', 'csv', 'sh'):
            r = run(mod._call_tool(None, _tc('deliverable', {'format': bad, 'run_id': 'run-a'})))
            self.assertTrue(r.is_error)
            self.assertIn('md, xlsx, pdf', r.content[0].text)


class UnchangedSurfaces(unittest.TestCase):
    """13-16. Other profiles and Jev untouched."""

    def test_research_profiles_unmodified(self):
        base = Path.home() / '.hermes/profiles'
        for name in ('lg-find-maps', 'lg-read-site', 'lg-supervisor', 'lg-validator'):
            cfg = base / name / 'config.yaml'
            if cfg.exists():
                import yaml
                c = yaml.safe_load(cfg.read_text())
                self.assertEqual(c.get('mcp_servers'), {}, name)

    def test_jev_shadow_flags_default_off(self):
        os.environ.pop('JEV_SHADOW_ENABLED', None)
        import jev_shadow as JS
        self.assertFalse(JS.enabled())

    def test_jev_shadow_files_untouched(self):
        src = (ROOT / 'app' / 'jev_shadow.py').read_text()
        self.assertIn("os.environ.get('JEV_SHADOW_ENABLED') == 'true'", src)
        sandbox = (ROOT / 'app' / 'sandbox.py').read_text()
        self.assertIn("os.environ.get('JEV_SHADOW_ENABLED')=='true'", sandbox)


if __name__ == '__main__':
    unittest.main()
