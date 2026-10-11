"""Sandbox policy regression: the home-directory read denial.

`app/sandbox.py` is the fail-closed boundary around every local worker. Its
first substantive rule denies `file-read-data` under the current user's home
directory, so that a worker can only read the paths the policy re-allows
afterwards (the release tree, its own Hermes profile, the run directory, and
for browser roles the shared caches). That denial used to be written as a
hard-coded absolute path; it is now derived from `Path.home()`.

These tests hold both ends: the generated policy must deny reads under the
current user's home, and it must derive that path rather than embed one. A
weakened or dropped denial is a boundary regression, not a cosmetic one.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import sandbox  # noqa: E402

# Roles that between them cover every branch of the policy builder.
ROLES = ('read-google', 'validator', 'render-md', 'consolidator', 'supervisor', 'resolve-owner')


class HomeDirectoryDenial(unittest.TestCase):
    def policy(self, role):
        # policy() only composes strings; no path it names needs to exist.
        return sandbox.policy(ROOT / 'runs' / 'sandbox-policy-probe', role)

    def test_generated_profile_denies_reads_under_the_current_user_home(self):
        home = json.dumps(str(Path.home()))
        expected = '(deny file-read-data (subpath ' + home + '))'
        for role in ROLES:
            with self.subTest(role=role):
                self.assertIn(expected, self.policy(role))

    def test_home_denial_is_derived_from_the_environment_not_embedded(self):
        source = (ROOT / 'app' / 'sandbox.py').read_text()
        self.assertIn('Path.home()', source, 'the home denial must be derived at runtime')
        self.assertNotIn('/Users/', source, 'no absolute macOS home path may be embedded')

    def test_home_denial_precedes_the_allowed_read_paths(self):
        # Sandbox rules are last-match-wins, so the re-allowing reads must come
        # after the denial; the reverse order would silently disable it.
        for role in ROLES:
            with self.subTest(role=role):
                lines = self.policy(role).splitlines()
                deny = next(i for i, l in enumerate(lines) if l.startswith('(deny file-read-data (subpath'))
                allows = [i for i, l in enumerate(lines) if l.startswith('(allow file-read-data ')]
                self.assertTrue(all(i > deny for i in allows))

    def test_write_denial_and_carve_outs_are_intact(self):
        text = self.policy('validator')
        self.assertIn('(deny file-write*)', text)
        # The denial is a default, not a blanket: the worker's own staging and
        # the immutable control directory must remain writable.
        self.assertIn('(allow file-write*', text)

    def test_every_role_still_carries_a_home_denial(self):
        # Guard against a role being added to the policy builder without the
        # denial, which is how a boundary quietly reopens.
        import core
        home = json.dumps(str(Path.home()))
        for role in sorted(core.PROFILES):
            with self.subTest(role=role):
                self.assertIn('(deny file-read-data (subpath ' + home + '))', self.policy(role))


if __name__ == '__main__':
    unittest.main()
