"""Run the macOS sandbox boundary tests, failing unless both actually ran.

These two tests are the only place the fail-closed SBPL policy is executed
against real `sandbox-exec`, so they can only run on macOS and are skipped
everywhere else. That makes them easy to lose: a re-added platform guard or a
renamed test would turn the macOS CI job green while nothing was exercised. Run
them through here so a skip is a failure, and report what was run.
"""
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / 'app'))
sys.path.insert(0, str(TESTS))

import test_runtime  # noqa: E402

NAMES = ['test_os_boundaries', 'test_renderer_raw_denied']


def main():
    suite = unittest.TestSuite(test_runtime.Runtime(name) for name in NAMES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    missed = [name for name, _ in result.skipped]
    problems = []
    if result.testsRun != len(NAMES):
        problems.append(f'ran {result.testsRun} of {len(NAMES)} sandbox boundary tests')
    if missed:
        problems.append('skipped: ' + ', '.join(missed))
    if result.failures or result.errors:
        problems.append('failed or errored')
    if problems:
        print('sandbox boundary coverage incomplete: ' + '; '.join(problems), file=sys.stderr)
        return 1
    print(f'{result.testsRun} sandbox boundary tests exercised the fail-closed policy')
    return 0


if __name__ == '__main__':
    sys.exit(main())
