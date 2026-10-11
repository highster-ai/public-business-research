import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from limits import COUNT_CAP_ERROR, MAX_COUNT, validate_count
import pipeline


def spec(count):
    return {
        "category": "spa",
        "area": "Dubai Marina",
        "country": "AE",
        "count": count,
        "sources": ["web"],
        "requested_fields": ["business_name"],
        "dm_probe": False,
        "formats": ["md"],
    }


class CountLimitTests(unittest.TestCase):
    def test_schema_maximums_are_generated_from_shared_limit(self):
        run_schema = json.loads((ROOT / "schemas" / "run.json").read_text())
        canonical_schema = json.loads((ROOT / "schemas" / "canonical.json").read_text())
        self.assertEqual(run_schema["properties"]["count"]["maximum"], MAX_COUNT)
        self.assertEqual(canonical_schema["properties"]["spec"]["properties"]["count"]["maximum"], MAX_COUNT)
        subprocess.run([sys.executable, str(ROOT / "app" / "generate_schemas.py"), "--check"], check=True)

    def test_runtime_boundary_60_accepted_61_rejected_with_canonical_message(self):
        self.assertEqual(validate_count(60), 60)
        with self.assertRaisesRegex(ValueError, "^" + COUNT_CAP_ERROR + "$"):
            validate_count(61)

    def test_both_schemas_accept_60_and_reject_61(self):
        run_schema = json.loads((ROOT / "schemas" / "run.json").read_text())
        canonical_schema = json.loads((ROOT / "schemas" / "canonical.json").read_text())
        jsonschema.validate(spec(60), run_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(spec(61), run_schema)
        canonical = {"run_id": "run-test", "spec": spec(60), "businesses": [], "evidence": [], "resolutions": []}
        # Other canonical constraints are irrelevant here: validate the generated count node directly.
        count_schema = canonical_schema["properties"]["spec"]["properties"]["count"]
        jsonschema.validate(canonical["spec"]["count"], count_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(61, count_schema)

    def test_direct_pipeline_cli_rejects_61_before_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "spec.json"
            path.write_text(json.dumps(spec(61)))
            result = subprocess.run(
                [sys.executable, str(ROOT / "app" / "pipeline.py"), "preflight", str(path)],
                cwd=ROOT, capture_output=True, text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(COUNT_CAP_ERROR, result.stderr)

    def test_direct_pipeline_cli_accepts_60_at_count_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "spec.json"
            path.write_text(json.dumps(spec(60)))
            argv = ["pipeline.py", "preflight", str(path)]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(pipeline, "preflight", return_value={"passed": True}) as preflight:
                pipeline.main()
        self.assertEqual(preflight.call_args.args[0]["count"], MAX_COUNT)


if __name__ == "__main__":
    unittest.main()
