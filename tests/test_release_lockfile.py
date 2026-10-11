"""Guard the container lockfile against the `pip install --no-deps` gap.

Dockerfile.worker installs requirements.lock with --no-deps (the base image
supplies Hermes' own transitive set). Any runtime dependency needed by an
application package but NOT already present in the base image must therefore
be pinned explicitly, or the worker image imports fail -- e.g. openpyxl needs
et-xmlfile and pdfplumber needs pdfminer.six, both absent from the digest-pinned
base. A missing pin only surfaces inside the container (render-xlsx/pdf), so
this lock-level check is the synthetic regression guard.
"""
import re, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
# Transitive runtime deps NOT provided by the digest-pinned base image.
REQUIRED_TRANSITIVE={'et-xmlfile','pdfminer.six'}


class LockfileTests(unittest.TestCase):
 def setUp(self):
  self.lines=[l.strip() for l in (ROOT/'requirements.lock').read_text().splitlines()]
  self.reqs=[l for l in self.lines if l and not l.startswith('#')]

 def test_transitive_runtime_requirements_are_pinned(self):
  names={re.split(r'==',l,1)[0].strip().lower() for l in self.reqs if not l.startswith('-e')}
  for name in REQUIRED_TRANSITIVE:
   self.assertIn(name,names)

 def test_every_locked_requirement_is_exactly_pinned(self):
  for l in self.reqs:
   if l.startswith('-e'):continue # the pinned immutable Hermes git requirement
   self.assertRegex(l,r'^[A-Za-z0-9_.\-]+==\S+$')


if __name__=='__main__':unittest.main()
