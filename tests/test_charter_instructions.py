"""Generated role-instruction contract.

`app/setup.py` writes each isolated profile's SKILL.md from `app/charters.py`.
The instruction text is part of the release's safety contract: a page that
denies access -- a login wall, a consent wall, a restricted view or a
limited-access page -- is a source restriction. The host records the fields it
blocks as `access_restricted`. It must never be reported as `not_public`, which
would assert the business does not publish the value rather than that we could
not see it.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import charters  # noqa: E402

# The access-denying conditions, spelled out here rather than read from the
# module, so that dropping one from the generated text fails this test.
CONDITIONS = ('login wall', 'consent wall', 'restricted view', 'limited-access page')

EXPECTED_ROLES = (
    'supervisor intake query-plan find-maps find-web dedupe read-google read-site '
    'find-ig read-ig read-reviews find-owner resolve-ig resolve-wa resolve-owner '
    'resolve-dm probe-send probe-check validator consolidator render-xlsx render-pdf '
    'render-md'.split()
)


class GeneratedCharterInstructions(unittest.TestCase):
    def test_every_role_gets_a_restriction_aware_charter(self):
        self.assertEqual(charters.ROLES, EXPECTED_ROLES)
        for role in charters.ROLES:
            with self.subTest(role=role):
                text = charters.role_instruction(role)
                self.assertIn('access_restricted', text)
                lowered = text.lower()
                for condition in CONDITIONS:
                    self.assertIn(condition, lowered)
                self.assertIn('never report them as not_public', text)

    def test_no_charter_calls_a_wall_not_public(self):
        # The regression: the read-ig charter used to read "Login walls are
        # not_public", which asserts the business publishes no handle when the
        # truth is that a login gate hid it. No role may say that.
        for role in charters.ROLES:
            with self.subTest(role=role):
                text = charters.role_instruction(role)
                self.assertNotIn('are not_public', text)
                self.assertNotIn('is not_public', text)
                self.assertNotIn('restrictions are not_public', text)

    def test_shared_contract_is_identical_across_roles(self):
        # Every role carries the same shared paragraph: a reader, a resolver and
        # a renderer must not disagree about what a restriction means.
        shared = {charters.role_instruction(r).split('\n\n', 2)[2] for r in charters.ROLES}
        self.assertEqual(len(shared), 1)

    def test_front_matter_names_the_skill_that_is_created(self):
        # setup.py creates the directory leads-<role>, except the Markdown
        # renderer whose skill is leads-report-md; the front-matter must match.
        self.assertEqual(charters.skill_name('render-md'), 'leads-report-md')
        self.assertEqual(charters.skill_name('read-google'), 'leads-read-google')
        self.assertIn('name: leads-report-md', charters.role_instruction('render-md'))
        self.assertIn('name: leads-read-google', charters.role_instruction('read-google'))

    def test_agent_roles_are_readers_only_where_a_browser_is_granted(self):
        # The browser-bearing roles are exactly the ones that can meet a wall.
        self.assertEqual(set(charters.READERS) <= set(charters.ROLES), True)
        self.assertIn('read-google', charters.READERS)
        self.assertIn('read-reviews', charters.READERS)
        self.assertNotIn('validator', charters.READERS)


import re  # noqa: E402

# Conditions that deny access, and the negated forms that are allowed to name
# not_public because they forbid it.
WALL_TERM = re.compile(
    r'login wall|consent wall|restricted view|limited-access page|login restriction',
    re.I)
NEGATED = re.compile(r'never\b[\s\S]{0,60}?not_public', re.I)


class NoSourceMapsAWallToNotPublic(unittest.TestCase):
    """The same rule as the charters, enforced over every app source file.

    A model-facing string anywhere in app/ -- a resolver prompt, an operator
    hint, a docstring -- that answers `not_public` for a wall reintroduces the
    release blocker the charter text fixes, so guard the whole tree, not just
    the generated instructions.
    """

    def test_no_app_source_maps_a_wall_to_not_public(self):
        sources = sorted((ROOT / 'app').glob('*.py'))
        self.assertGreater(len(sources), 5)
        for path in sources:
            with self.subTest(source=path.name):
                text = NEGATED.sub('NEGATED_WALL_STATUS', path.read_text())
                for match in WALL_TERM.finditer(text):
                    window = text[match.start():match.start() + 160]
                    self.assertNotIn('not_public', window.lower(),
                                     f'{path.name} maps a wall to not_public: {window!r}')


if __name__ == '__main__':
    unittest.main()
