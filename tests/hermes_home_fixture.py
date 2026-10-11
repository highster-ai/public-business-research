"""A throwaway HERMES_HOME for tests that need one.

Preflight audits every profile's `config.yaml`, and the dedicated-Chrome
launcher rewrites its own profile's `config.yaml`, so both read the isolated
Hermes home. They used to read whichever HERMES_HOME the developer happened to
have: the tests passed on a provisioned machine and failed on a fresh runner,
which made the suite depend on ambient state rather than on the release tree.

Build the directory structure those code paths expect in a temporary home and
redirect `core.HERMES_HOME` at it for the duration of a test. The real home is
never consulted. `profile_charter` resolves its relative charter paths through
that module global at call time, so redirecting it redirects every reader --
preflight, chrome, sandbox -- without patching each of them.
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml


def profile_config(model):
    """The profile config the release's own setup writes, as a plain dict."""
    return {
        'model': model,
        'toolsets': [],
        'platform_toolsets': {'cli': []},
        'agent': {'max_turns': 35},
        'browser': {'backend': 'local', 'use_real_profile': False, 'headed': True,
                    'restrict_evaluate': True, 'extension_control': {'enabled': False}},
        'kanban': {'auto_decompose': False, 'max_in_progress': 4,
                   'max_in_progress_per_profile': 1, 'failure_limit': 2},
        'memory': {'memory_enabled': False, 'user_profile_enabled': False},
        'terminal': {'backend': 'local', 'cwd': '.'},
        'mcp_servers': {},
    }


def write_profile(home, role, manifest):
    """Write one isolated profile where `core.profile_charter` looks for it."""
    charter = home / manifest['charter']
    charter.parent.mkdir(parents=True, exist_ok=True)
    charter.write_text(f'---\nname: fixture-{role}\n---\nfixture charter\n')
    config = charter.parents[2] / 'config.yaml'
    config.write_text(yaml.safe_dump(profile_config(manifest['model'])))
    return config


def build(home, profiles, roles=None):
    """Materialise `profiles` under `home` and return the home."""
    for role, manifest in profiles.items():
        if roles is None or role in roles:
            write_profile(home, role, manifest)
    return home


def install(case, roles=None):
    """Build a temporary Hermes home for `case` and redirect core at it.

    Call from `setUp`: the patch and the temporary directory are torn down
    with the test.
    """
    import core
    # Resolved, because core.HERMES_HOME is, and because every path derived
    # from it is resolved on the way out. On macOS the temporary directory
    # lives under /var, a symlink to /private/var, so an unresolved home here
    # would not contain the paths the code under test actually returns.
    home = Path(tempfile.mkdtemp(prefix='hermes-home-')).resolve()
    build(home, core.PROFILES, roles)
    redirect = patch.object(core, 'HERMES_HOME', home)
    redirect.start()
    case.addCleanup(redirect.stop)
    return home
