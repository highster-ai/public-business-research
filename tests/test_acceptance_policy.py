import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import pipeline  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "run-20261010-160848-coverage.json"


def sparse_dataset():
    fixture = json.loads(FIXTURE.read_text())
    businesses = [
        {"business_id": f"b_{i:02d}", "name_raw": f"Business {i}", "url": None, "evidence_ids": []}
        for i in range(fixture["business_count"])
    ]
    rows = []
    for field, counts in fixture["field_status_counts"].items():
        business_index = 0
        for status, count in counts.items():
            for _ in range(count):
                rows.append({
                    "business_id": businesses[business_index]["business_id"],
                    "field": field,
                    "status": status,
                    "value": "fixture" if status == "found" else None,
                })
                business_index += 1
    spec = {
        "count": fixture["business_count"],
        "country": "ID",
        "requested_fields": fixture["requested_fields"],
    }
    return spec, {
        "run_id": fixture["source_run"],
        "spec": spec,
        "businesses": businesses,
        "evidence": [],
        "resolved_fields": rows,
        "validation": {"valid": True, "errors": [], "checked_at": "fixture"},
        "source_priority": {},
        "approved": False,
    }


class AcceptancePolicyTests(unittest.TestCase):
    def make_run(self, data):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        run = Path(td.name)
        for directory in ["canonical", "control", "reports", "audit"]:
            (run / directory).mkdir()
        (run / "canonical" / "leads.json").write_text(json.dumps(data))
        return run

    def test_sparse_twenty_business_regression_is_partial_success(self):
        spec, data = sparse_dataset()
        run = self.make_run(data)

        result = pipeline.gate(run, spec)

        self.assertEqual(result["state"], "partial_success")
        self.assertFalse(result["approved"])
        self.assertEqual(result["field_coverage"]["google_rating"]["resolved_percent"], 5.0)
        self.assertEqual(result["field_coverage"]["google_review_count"]["failed_percent"], 100.0)
        self.assertEqual(result["field_coverage"]["public_contact"]["found_percent"], 0.0)
        self.assertEqual(
            result["warnings"],
            ["instagram below 70% resolved", "whatsapp below 70% resolved"],
        )
        report = (run / "reports" / "run-report.md").read_text()
        self.assertIn("State: partial_success", report)
        self.assertIn("google_rating", report)
        self.assertIn("Resolved: 5.0%", report)

    def test_default_policy_matches_release_contract(self):
        policy = pipeline.load_acceptance_policy()
        self.assertEqual(
            policy["gate_fields"],
            ["google_rating", "google_review_count", "public_contact"],
        )
        self.assertEqual(policy["minimum_resolved_percent"], 70)
        self.assertEqual(policy["soft_fields"], ["instagram", "whatsapp"])
        self.assertEqual(policy["informational_fields"], ["owner", "dm_proxy"])

    def test_iteration_exhaustion_forces_partial_success_and_is_recorded(self):
        spec, data = sparse_dataset()
        for row in data["resolved_fields"]:
            if row["field"] in {"google_rating", "google_review_count", "public_contact"}:
                row["status"] = "found"
                row["value"] = "fixture"
        run = self.make_run(data)
        log = run / "reports" / "find-maps.log"
        log.write_text("Iteration budget exhausted (32/32)\nReached maximum iterations (32)\n")

        self.assertTrue(pipeline.record_iteration_exhaustion(run, "find-maps", "find-maps"))
        result = pipeline.gate(run, spec)

        self.assertEqual(result["state"], "partial_success")
        self.assertEqual(result["iteration_exhausted_tasks"], ["find-maps"])
        incomplete = json.loads((run / "reports" / "incomplete" / "find-maps.json").read_text())
        self.assertEqual(incomplete["reason"], "iteration_exhausted")

    def test_gate_fields_at_threshold_are_approved(self):
        spec, data = sparse_dataset()
        gate_fields = {"google_rating", "google_review_count", "public_contact"}
        for field in gate_fields:
            rows = [r for r in data["resolved_fields"] if r["field"] == field]
            for index, row in enumerate(rows):
                row["status"] = "found" if index < 14 else "not_found"
                row["value"] = "fixture" if index < 14 else None
        run = self.make_run(data)

        result = pipeline.gate(run, spec)

        self.assertEqual(result["state"], "approved")
        self.assertTrue(result["approved"])

    def test_just_below_threshold_is_partial_success(self):
        spec, data = sparse_dataset()
        data["businesses"] = [{"business_id": f"b_{i:04d}", "name_raw": f"Business {i}", "url": None, "evidence_ids": []} for i in range(1000)]
        data["resolved_fields"] = []
        for field in spec["requested_fields"]:
            for index, business in enumerate(data["businesses"]):
                status = "found" if field in {"google_rating", "google_review_count", "public_contact"} and index < 699 else "not_found"
                data["resolved_fields"].append({"business_id": business["business_id"], "field": field, "status": status, "value": "fixture" if status == "found" else None})
        spec["count"] = 1000
        run = self.make_run(data)
        result = pipeline.gate(run, spec)
        self.assertEqual(result["field_coverage"]["google_rating"]["resolved_percent"], 69.9)
        self.assertEqual(result["state"], "partial_success")

    def test_not_public_counts_as_resolved_and_informational_zero_does_not_gate(self):
        spec, data = sparse_dataset()
        for row in data["resolved_fields"]:
            if row["field"] in {"google_rating", "google_review_count", "public_contact"}:
                row["status"] = "not_public"
                row["value"] = None
            if row["field"] in {"owner", "dm_proxy"}:
                row["status"] = "not_found"
                row["value"] = None
        result = pipeline.gate(self.make_run(data), spec)
        self.assertEqual(result["field_coverage"]["public_contact"]["resolved_percent"], 100.0)
        self.assertEqual(result["field_coverage"]["owner"]["resolved_percent"], 0.0)
        self.assertEqual(result["state"], "approved")

    def test_error_status_is_failed(self):
        spec, data = sparse_dataset()
        data["resolved_fields"][0]["status"] = "error"
        run = self.make_run(data)
        with self.assertRaisesRegex(ValueError, "Unresolved error fields"):
            pipeline.gate(run, spec)
        self.assertEqual(json.loads((run / "reports" / "run-state.json").read_text())["state"], "failed")

    def test_partial_success_banner_and_coverage_are_first_in_each_format(self):
        spec, data = sparse_dataset()
        for row in data["resolved_fields"]:
            row.setdefault("evidence_ids", [])
            row.setdefault("confidence", 0)
        run = self.make_run(data)
        (run / "deliverables").mkdir()
        pipeline.gate(run, spec)
        charter = run / "bundle" / "skill" / "SKILL.md"
        charter.parent.mkdir(parents=True)
        charter.write_text("test")
        for fmt in ("xlsx", "pdf"):
            bundle = run / ("bundle-" + fmt)
            (bundle / "scripts").mkdir(parents=True)
            (bundle / "SKILL.md").write_text("test")
            captured = {}
            def fake_run(argv, input=None, **kwargs):
                captured["spec"] = json.loads(input) if input else json.loads((run / "deliverables" / "pdf-spec.json").read_text())
                Path(argv[-1]).write_bytes(b"artifact")
            with mock.patch.dict("os.environ", {"LEADGEN_CHARTER": str(charter), "LEADGEN_RENDER_BUNDLE": str(bundle)}), mock.patch.object(pipeline.subprocess, "run", side_effect=fake_run):
                pipeline.render(run, fmt)
            if fmt == "xlsx":
                self.assertEqual(captured["spec"]["sheets"][0]["name"], "Summary")
                self.assertEqual(captured["spec"]["sheets"][0]["rows"][0], ["Run state", "partial_success"])
                self.assertEqual(captured["spec"]["sheets"][0]["rows"][3][0], "Field")
            else:
                self.assertEqual(captured["spec"]["elements"][0]["text"], "STATE: partial_success")
                self.assertEqual(captured["spec"]["elements"][1]["text"], "Coverage summary")
        with mock.patch.dict("os.environ", {"LEADGEN_CHARTER": str(charter)}):
            pipeline.render(run, "md")
        markdown = (run / "deliverables" / "leads.md").read_text()
        self.assertTrue(markdown.startswith("# STATE: partial_success"))
        self.assertIn("## Coverage summary", markdown)

    def test_count_failure_records_failed_state(self):
        spec, data = sparse_dataset()
        data["businesses"].pop()
        run = self.make_run(data)

        with self.assertRaisesRegex(ValueError, "Acceptance gate"):
            pipeline.gate(run, spec)

        state = json.loads((run / "reports" / "run-state.json").read_text())
        self.assertEqual(state["state"], "failed")


if __name__ == "__main__":
    unittest.main()
