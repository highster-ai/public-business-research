"""No test may read the real home or the real Hermes home.

The suite has to pass on a bare runner: no provisioned ~/.hermes, no isolated
profiles, nothing under the user's home a test can lean on. Three tests were
reading the ambient Hermes home for profile configuration and so passed on a
provisioned machine while failing in CI; that is the failure this guards.

Tests that need a Hermes home build a throwaway one with hermes_home_fixture.
Tests that need macOS say so and are run by the macOS job. Direct references to
the ambient Hermes home in the remaining modules are the release-tree probes
listed in AMBIENT_PROBES: they ask whether a path exists and skip when it does
not, which is a property of the release rather than a dependence on the
machine. Anything new is a decision, so it has to be recorded here.
"""
import ast
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / 'app'))
sys.path.insert(0, str(TESTS))
import hermes_home_fixture  # noqa: E402

# Each entry: module -> how many times it may name the ambient Hermes home.
AMBIENT_PROBES = {
    'test_docker_execution.py': 1,      # asserts the container mounts no host home
    'test_leadgen_control_mcp.py': 3,   # skip when the profile is not in the tree
    'test_orchestrator.py': 4,          # skip when the profile is not in the tree
}
# Redirecting that variable is the fixture's job, and this module has to name
# it to say so.
ALLOWED_HERMES_HOME = {'hermes_home_fixture.py', 'test_home_hygiene.py'}
# Modules that must build their own Hermes home rather than use the developer's.
PROFILE_DEPENDENT = ('test_docker_execution.py', 'test_concurrency_patch.py', 'test_orchestration.py')


def _tree(source):
    return ast.parse(source.read_text())


def ambient_home_reads(source):
    """Count `Path.home() / '.hermes...'` expressions, ignoring prose."""
    found = 0
    for node in ast.walk(_tree(source)):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        right, left = node.right, node.left
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)
                and right.value.startswith('.hermes')):
            continue
        if (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                and left.func.attr == 'home' and isinstance(left.func.value, ast.Name)
                and left.func.value.id == 'Path'):
            found += 1
    return found


def names_the_hermes_home_variable(source):
    return any(isinstance(node, ast.Name) and node.id == 'HERMES_HOME' for node in ast.walk(_tree(source)))


class NothingReadsTheAmbientHome(unittest.TestCase):
    def test_only_the_recorded_probes_name_the_ambient_hermes_home(self):
        found = {}
        for source in sorted(TESTS.glob('test_*.py')):
            hits = ambient_home_reads(source)
            if hits:
                found[source.name] = hits
        self.assertEqual(found, AMBIENT_PROBES,
                         'a test reads the ambient Hermes home; build a throwaway one with '
                         'hermes_home_fixture, or record the probe in AMBIENT_PROBES')

    def test_only_the_fixture_redirects_hermes_home(self):
        offenders = [source.name for source in sorted(TESTS.glob('*.py'))
                      if source.name not in ALLOWED_HERMES_HOME and names_the_hermes_home_variable(source)]
        self.assertEqual(offenders, [], 'HERMES_HOME belongs to the fixture, not to individual tests')

    def test_profile_dependent_modules_build_their_own_home(self):
        for name in PROFILE_DEPENDENT:
            with self.subTest(module=name):
                text = (TESTS / name).read_text()
                self.assertIn('hermes_home_fixture.install(self)', text)


class TheFixtureMovesTheLookupOffTheRealHome(unittest.TestCase):
    def test_profile_lookup_resolves_inside_the_throwaway_home(self):
        import core
        real = core.HERMES_HOME
        home = hermes_home_fixture.install(self)
        self.assertNotEqual(home, real)
        self.assertTrue(home.name.startswith('hermes-home-'))
        charter = core.profile_charter('supervisor')
        self.assertTrue(charter.is_file(), 'the fixture must write the charter it is asked for')
        self.assertTrue(charter.is_relative_to(home))
        self.assertFalse(charter.is_relative_to(real))


if __name__ == '__main__':
    unittest.main()
