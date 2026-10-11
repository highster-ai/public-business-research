"""`.gitignore` contract.

The release checkout is an allowlist: `*` ignores everything, a short list of
release files is unignored, and explicit denials follow. The risk this test
guards is a generic allow pattern (for example `*.json` or `*.yaml` at the
repository root) silently admitting a credential file that happens to land
next to the configuration it resembles.

For every path a denial must cover -- at the root and nested -- the test creates
a real file and asserts `git check-ignore` matches it. It also asserts that each
file the release must ship is *not* ignored, and that the root allowlist still
matches what is actually tracked.
"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args, cwd=ROOT):
    return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True)


def repo_root():
    r = git('rev-parse', '--show-toplevel')
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


REPO = repo_root()

# Files that must never be committed. Root-level and nested copies, because a
# denial that only pins the root still leaks a stray `app/credentials.json`.
FORBIDDEN = [
    # Environment files, including the non-example variants.
    '.env', '.env.local', '.env.production', '.env.secret',
    'app/.env', 'tests/.env.local',
    # Credentials, secrets and tokens.
    'credentials.json', 'secrets.json', 'token.json', 'service-account.json', 'private.json',
    'app/credentials.json', 'tests/tmp/secrets.json', 'docs/private.json', 'schemas/token.json',
    'app/nested/deep/service-account.json',
    # Cookies and browser profiles.
    'cookies.txt', 'cookies.json', 'session.cookie', 'browser/Cookies', 'app/browser/Cookies',
    'profiles/Default/Cookies', 'profile-bridge/state.json',
    # Local run state, experiments and captured snapshots.
    'runs/x.json', 'runs/run-1/raw/read-google/snapshots/s_abc.json',
    'experiments/e.txt', 'snapshots/s.json', 'app/site/snapshots/deep.json',
    'diagnostics/d.txt', 'backups/config.yaml',
    # Generated deliverables and outputs.
    'deliverables/leads.md', 'app/deliverables/leads.xlsx', 'out.jsonl', 'app/trace.jsonl',
    'app/out.xlsx', 'app/report.pdf', 'leads.md', 'app/leads.csv', 'leads.xlsx',
]

# The release's root files. This list is the allowlist in .gitignore spelled
# out; the test fails if a root file is added or removed without updating both.
REQUIRED_ROOT = sorted([
    '.env.example', '.gitleaks.toml', '.gitignore',
    'CONTRIBUTING.md', 'Dockerfile.worker', 'LICENSE', 'README.md', 'SECURITY.md',
    'acceptance-policy.json', 'example-backup.yaml', 'example.yaml',
    'leadgen', 'leadgen_control_mcp.py', 'model-policy.json', 'orchestrate',
    'profiles.json', 'requirements.lock', 'requirements.txt', 'smoke.yaml',
])


@unittest.skipIf(REPO is None, 'not a git checkout')
class GitignoreContract(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.dirs = []

    def tearDown(self):
        for f in reversed(self.created):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        for d in reversed(self.dirs):
            try:
                d.rmdir()
            except OSError:
                pass  # not empty, or never created: leave the tree alone

    def make(self, rel):
        '''Create a real file at `rel`, remembering it for cleanup.'''
        path = REPO / rel
        if not path.parent.exists():
            missing = []
            p = path.parent
            while p != REPO and not p.exists():
                missing.append(p)
                p = p.parent
            for d in reversed(missing):
                d.mkdir()
                self.dirs.append(d)
        path.write_text('placeholder written by tests/test_gitignore_contract.py\n')
        self.created.append(path)
        return path

    def ignored(self, rel):
        # --no-index asks the pattern rules alone, independent of what happens
        # to be tracked right now.
        r = git('check-ignore', '-q', '--no-index', '--', rel)
        self.assertIn(r.returncode, (0, 1), f'git check-ignore failed for {rel}: {r.stderr}')
        return r.returncode == 0

    def test_forbidden_files_are_ignored_and_never_addable(self):
        for rel in FORBIDDEN:
            with self.subTest(path=rel):
                self.make(rel)
                self.assertTrue(self.ignored(rel), f'{rel} is not ignored')
                added = git('add', '--dry-run', '--', rel)
                self.assertEqual(added.stdout.strip(), '', f'{rel} would be added')

    def test_required_root_files_are_not_ignored(self):
        for rel in REQUIRED_ROOT:
            with self.subTest(path=rel):
                self.assertFalse(self.ignored(rel), f'required file {rel} is ignored')

    def test_root_allowlist_matches_what_is_tracked(self):
        # A root file that is not in REQUIRED_ROOT has no exact-name allow rule
        # in .gitignore, so it would be silently dropped from the release.
        tracked = git('ls-files').stdout.split('\n')
        root_files = sorted(f for f in tracked if f and '/' not in f)
        self.assertEqual(root_files, REQUIRED_ROOT)

    def test_env_example_is_the_only_env_exception(self):
        # .env.example is a real tracked file; assert the pattern rules leave it
        # alone rather than writing over it.
        self.assertFalse(self.ignored('.env.example'), '.env.example must ship')
        self.assertIn('.env.example', REQUIRED_ROOT)
        for rel in ('.env', '.env.local', '.env.example.bak'):
            with self.subTest(path=rel):
                self.make(rel)
                self.assertTrue(self.ignored(rel), f'{rel} is not ignored')

    def test_no_generic_root_wildcard_remains(self):
        lines = [l.strip() for l in (ROOT / '.gitignore').read_text().splitlines()]
        for pattern in ('!/*.json', '!/*.yaml', '!/*.yml', '!/*.toml'):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, lines, f'generic root allow {pattern} is back')

    def test_denials_come_after_every_allow_rule(self):
        # A denial placed before the allowlist would be overridden by it.
        lines = [l.strip() for l in (ROOT / '.gitignore').read_text().splitlines()]
        content = [l for l in lines if l and not l.startswith('#')]
        first_denial = content.index('/.env')
        allows = [i for i, l in enumerate(content) if l.startswith('!') and l != '!/.env.example']
        self.assertTrue(all(i < first_denial for i in allows),
                        'an allow rule appears after the first denial')


if __name__ == '__main__':
    unittest.main()
