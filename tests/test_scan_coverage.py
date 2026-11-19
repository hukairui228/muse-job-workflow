import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scan_coverage.py"
SPEC = importlib.util.spec_from_file_location("scan_coverage", SCRIPT)
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.ledger = {"schemaVersion": 1, "sources": {}}

    def capture(self, count=1, total=1, **extra):
        snapshot = {"url": "https://example.com/jobs?level=junior", "total": total,
                    "rows": [{"index": i + 1, "sourceId": str(i), "title": "Analyst"}
                             for i in range(count)], "complete": True,
                    "endEvidence": "Footer: no more results", "pages": 1}
        snapshot.update(extra)
        coverage.ingest(self.ledger, "finance", snapshot, "/capture.json")
        return self.ledger["sources"]["finance"]

    def status(self):
        return coverage.report(self.ledger)["sources"][0]

    def test_105_of_407_cannot_finish_even_if_declared_complete(self):
        self.capture(105, 407)
        self.assertFalse(self.status()["listComplete"])
        self.assertFalse(self.status()["allComplete"])
        self.assertEqual(self.status()["missingIdentifiedItems"], 302)

    def test_407_and_end_is_only_list_complete_not_classified(self):
        self.capture(407, 407)
        self.assertTrue(self.status()["listComplete"])
        self.assertFalse(self.status()["classificationComplete"])
        self.assertEqual(len(self.status()["unclassified"]), 407)
        self.assertFalse(self.status()["allComplete"])

    def test_reingest_is_idempotent(self):
        self.capture(5, 5)
        self.capture(5, 5)
        source = self.ledger["sources"]["finance"]
        self.assertEqual(len(source["items"]), 5)
        self.assertEqual(len(source["captures"]), 1)

    def test_rows_with_unresolvable_same_identity_are_not_inflated(self):
        self.capture(2, 2, rows=[{"index": 0, "title": "Accountant"},
                                {"index": 1, "title": "Accountant"}])
        self.assertEqual(self.status()["lastCaptureRows"], 2)
        self.assertEqual(self.status()["readCount"], 1)
        self.assertEqual(self.status()["repeatedIdentityRows"], 1)
        self.assertFalse(self.status()["listComplete"])

    def test_valid_block_never_complete(self):
        self.capture(blocked={"type": "rate_limit", "reason": "HTTP429",
                              "evidence": "Saved response429", "nextStep": "Wait then retry",
                              "retryAt": "2026-11-03T17:00:00Z"})
        self.assertFalse(self.status()["allComplete"])
        self.assertFalse(self.status()["listComplete"])
        self.assertTrue(self.status()["blocked"]["valid"])

    def test_ordinary_signup_popup_not_valid_security_block(self):
        self.capture(blocked={"type": "signup_promotion", "reason": "Join our newsletter",
                              "evidence": "Newsletter modal", "nextStep": "Ask login",
                              "retryAt": None})
        self.assertFalse(self.status()["blocked"]["valid"])
        self.assertFalse(self.status()["allComplete"])

    def test_unknown_not_ready(self):
        source = self.capture()
        with self.assertRaises(ValueError):
            coverage.classify(source, "0", "review_ready", "Finance title")
        self.assertEqual(len(self.status()["unclassified"]), 1)
        self.assertFalse(self.status()["allComplete"])

    def test_capture_preserves_classification(self):
        source = self.capture()
        coverage.classify(source, "0", "user_skip", "Candidate explicitly passed", "User message")
        self.capture(capturedAt="2026-11-03T17:00:00Z", rows=[{"sourceId": "0", "title": "Updated title"}])
        self.assertEqual(source["items"]["id:0"]["classification"]["disposition"], "user_skip")
        self.assertTrue(self.status()["allComplete"])

    def test_complete_without_end_evidence_is_not_enough(self):
        self.capture(endEvidence=True)
        self.assertFalse(self.status()["listComplete"])

    def test_econ_structured_end_evidence_supported(self):
        self.capture(endEvidence={"actualDOMCount": 1, "buttonHidden": True,
                                  "sequence": [1], "cuaResult": "Final DOM rows equal total"})
        self.assertTrue(self.status()["listComplete"])

    def test_fingerprint_ignores_relative_update_time(self):
        self.capture(rows=[{"title": "Accountant", "label": "Accountant at Firm",
                            "paragraphs": ["Firm", "Example City · Hybrid", "$50k · Updated 1 hour ago"]}])
        self.capture(rows=[{"title": "Accountant", "label": "Accountant at Firm",
                            "paragraphs": ["Firm", "Example City · Hybrid", "$50k · Updated 2 hours ago"]}])
        self.assertEqual(self.status()["readCount"], 1)

    def test_ready_verification_and_handoff_are_separate(self):
        source = self.capture()
        coverage.classify(source, "0", "review_ready", "Full JD fits",
                          jdReviewed=True, officialStatus="official_open",
                          officialApplicationEvidence="Official CXS canApply=true saved api.json")
        self.assertTrue(self.status()["officialVerificationComplete"])
        self.assertFalse(self.status()["handoffComplete"])
        coverage.classify(source, "0", "review_ready", "Full JD fits",
                          handoffEvidence="Muse received Req and started form, receipt.json")
        self.assertTrue(self.status()["allComplete"])

    def test_econ_id_url_rows_supported(self):
        self.capture(rows=[{"id": "econ123", "url": "https://example.com/job/123?utm_source=x", "title": "Accountant"}])
        self.assertIn("id:econ123", self.ledger["sources"]["finance"]["items"])
        self.capture(capturedAt="later", rows=[{"id": "econ123", "url": "https://example.com/job/123", "title": "Accountant"}])
        self.assertEqual(self.status()["readCount"], 1)

    def test_same_title_different_ids_are_not_merged(self):
        self.capture(count=2, total=2)
        self.assertEqual(self.status()["readCount"], 2)

    def test_fingerprint_upgrades_when_id_becomes_available(self):
        self.capture(rows=[{"title": "Analyst", "label": "Company"}])
        self.capture(capturedAt="later", rows=[{"id": "123", "title": "Analyst", "label": "Company"}])
        self.assertEqual(self.status()["readCount"], 1)
        self.assertEqual(coverage.find_item(self.ledger["sources"]["finance"], "123")["row"]["id"], "123")

    def test_unverified_disposition_cannot_be_completed_with_stale_evidence(self):
        source = self.capture()
        coverage.classify(source, "0", "needs_official_verification", "Must check current form",
                          jdReviewed=True, officialStatus="official_open",
                          officialApplicationEvidence="Historical old evidence")
        self.assertFalse(self.status()["officialVerificationComplete"])

    def test_hold_requires_complete_jd_review_for_verification_stage(self):
        source = self.capture()
        coverage.classify(source, "0", "qualification_hold", "Years requirement not resolved",
                          officialStatus="official_open", officialApplicationEvidence="canApply=true")
        self.assertFalse(self.status()["officialVerificationComplete"])

    def test_experience_gap_alone_is_not_a_terminal_hold(self):
        for disposition in ("qualification_hold", "hard_qualification_gap"):
            with self.subTest(disposition=disposition):
                source = self.capture()
                coverage.classify(source, "0", disposition, "JD says required 2 years",
                                  jdReviewed=True, officialStatus="official_open",
                                  officialApplicationEvidence="canApply=true")
                self.assertFalse(self.status()["officialVerificationComplete"])
                self.assertFalse(self.status()["allComplete"])

    def test_actual_truthful_application_obstacle_can_complete_hold_review(self):
        source = self.capture()
        coverage.classify(source, "0", "qualification_hold", "Required declaration cannot truthfully be checked",
                          jdReviewed=True, officialStatus="official_open",
                          officialApplicationEvidence="Actual official form exists",
                          nonProceedableEvidence="Form requires false yes to mandatory licence declaration; evidence screenshot saved")
        self.assertTrue(self.status()["officialVerificationComplete"])

    def test_truthful_stretch_can_be_handed_off_without_claiming_perfect_fit(self):
        source = self.capture()
        coverage.classify(source, "0", "review_ready", "Apply truthfully despite JD years gap",
                          jdReviewed=True, officialStatus="official_open",
                          officialApplicationEvidence="canApply=true",
                          applicationTier="stretch", qualificationGap="No evidence of 2 years Treasury",
                          handoffEvidence="Muse received exactReq and honest answer constraints")
        self.assertTrue(self.status()["allComplete"])
        self.assertEqual(coverage.find_item(source,"0")["classification"]["qualificationGap"], "No evidence of 2 years Treasury")

    def test_malformed_block_never_counts_as_completion(self):
        self.capture(blocked=True)
        self.assertFalse(self.status()["listComplete"])
        self.assertFalse(self.status()["blocked"]["valid"])

    def test_scope_cannot_silently_change(self):
        self.capture()
        with self.assertRaises(ValueError):
            self.capture(url="https://example.com/jobs?level=senior")

    def test_incremental_requires_seen_boundary(self):
        proof = {"previousWatermark": "2026-11-02", "reached": True,
                 "boundarySourceId": "old", "evidence": "Reached prior known posting"}
        self.capture(1, 407, watermarkProof=proof)
        self.assertFalse(self.status()["listComplete"])
        self.capture(1, 407, watermarkProof=dict(proof, boundarySourceId="0"))
        self.assertTrue(self.status()["listComplete"])
        self.assertFalse(self.status()["allComplete"])

    def test_official_closed_requires_official_not_403(self):
        source = self.capture()
        with self.assertRaises(ValueError):
            coverage.classify(source, "0", "official_closed", "Workday403")

    def test_geographic_out_of_scope_requires_reason_and_evidence(self):
        source = self.capture()
        with self.assertRaises(ValueError):
            coverage.classify(source, "0", "out_of_scope", "")
        with self.assertRaises(ValueError):
            coverage.classify(source, "0", "out_of_scope", "Other City onsite outside Example City scope")
        coverage.classify(source, "0", "out_of_scope", "Other City onsite outside Example Metro scope",
                          "Captured row location Other City, ON; job is explicitly onsite, not Canada remote")
        self.assertEqual(source["items"]["id:0"]["classification"]["scopeDimension"], "geography")
        self.assertTrue(self.status()["classificationComplete"])
        self.assertTrue(self.status()["allComplete"])

    def test_old_evidence_cannot_silently_justify_geographic_exclusion(self):
        source = self.capture()
        coverage.classify(source, "0", "qualification_hold", "Need eligibility review", "Unrelated evidence")
        with self.assertRaises(ValueError):
            coverage.classify(source, "0", "out_of_scope", "Exclude location")

    def test_cli_gate_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            capture = Path(tmp) / "capture.json"
            capture.write_text(json.dumps({"url": "https://example.com", "total": 2,
                                          "rows": [{"id": "one", "title": "Analyst"}],
                                          "complete": True, "endEvidence": "End"}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(coverage.main(["ingest", "--ledger", str(ledger), "--source", "a",
                                                "--snapshot", str(capture)]), 0)
                self.assertEqual(coverage.main(["gate", "--ledger", str(ledger), "--stage", "list"]), 1)

    def test_manifest_missing_source_fails_presence_even_if_existing_finished(self):
        source = self.capture()
        coverage.classify(source, "0", "user_skip", "Candidate passed", "User message")
        result = coverage.report(self.ledger, manifest={"requiredSources": ["finance", "prosple"]})
        self.assertFalse(result["sourcePresence"])
        self.assertEqual(result["missingSources"], ["prosple"])
        self.assertFalse(result["allComplete"])

    def test_manifest_correct_sources_pass_presence_not_other_layers(self):
        self.capture()
        result = coverage.report(self.ledger, manifest={"requiredSources": ["finance"]})
        self.assertTrue(result["sourcePresence"])
        self.assertTrue(result["sources"][0]["listComplete"])
        self.assertFalse(result["allComplete"])

    def test_manifest_present_but_blocked_source_is_not_complete(self):
        self.capture(blocked={"type": "verification", "reason": "Official verification challenge",
                              "evidence": "Saved challenge response", "nextStep": "User handles official challenge",
                              "retryCondition": "After official challenge resolved"})
        result = coverage.report(self.ledger, manifest={"requiredSources": ["finance"]})
        self.assertTrue(result["sourcePresence"])
        self.assertFalse(result["allComplete"])

    def test_manifest_cannot_be_empty_or_duplicate(self):
        self.capture()
        for manifest in [{"requiredSources": []}, {"requiredSources": [""]},
                         {"requiredSources": ["finance", "finance"]}, {}]:
            with self.assertRaises(ValueError):
                coverage.report(self.ledger, manifest=manifest)

    def test_manifest_source_filter_cannot_hide_other_required_source(self):
        source = self.capture()
        coverage.classify(source, "0", "user_skip", "Candidate passed", "User message")
        snapshot = {"url": "https://prosple.example", "total": 1,
                    "rows": [{"sourceId": "p1", "title": "Analyst"}], "complete": True,
                    "endEvidence": "Final row and no next page"}
        coverage.ingest(self.ledger, "prosple", snapshot, "/prosple.json")
        result = coverage.report(self.ledger, "finance", {"requiredSources": ["finance", "prosple"]})
        self.assertTrue(result["sourcePresence"])
        self.assertEqual(len(result["sources"]), 2)
        self.assertFalse(result["allComplete"])

    def test_cli_manifest_missing_source_fails_even_list_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            manifest = Path(tmp) / "manifest.json"
            self.capture()
            coverage.save_ledger(ledger, self.ledger)
            manifest.write_text(json.dumps({"requiredSources": ["finance", "intern-insider"]}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(coverage.main(["gate", "--ledger", str(ledger), "--stage", "list",
                                                "--manifest", str(manifest)]), 1)


if __name__ == "__main__":
    unittest.main()
