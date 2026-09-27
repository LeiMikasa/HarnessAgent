"""Verification-feedback eval must isolate the diagnostic signal."""

from __future__ import annotations

import shutil

from common import HarnessCase

from evals.verification_feedback import (
    BUG_CASES,
    DIAGNOSTIC_REPAIR,
    EXPECTED_FAILURE,
    FIXTURE,
    GENERIC_REPAIR,
    MOCK_REPAIRED,
    run_worker,
    seeded_source,
    verify,
)


class VerificationFeedbackEvalTests(HarnessCase):
    def test_trusted_verifier_fails_stub_and_accepts_reference(self):
        workspace = self.tmp / "fixture"
        shutil.copytree(FIXTURE, workspace)
        passed, detail = verify(workspace)
        self.assertFalse(passed)
        self.assertIn("NotImplementedError", detail)

        (workspace / "csv_line.py").write_text(MOCK_REPAIRED, encoding="utf-8")
        self.assertEqual(verify(workspace), (True, "PASS"))

    def test_mock_control_gets_equal_retry_budget_but_no_case_details(self):
        generic_dir = self.tmp / "generic"
        diagnostic_dir = self.tmp / "diagnostic"
        generic_dir.mkdir()
        diagnostic_dir.mkdir()
        generic = run_worker("generic", generic_dir, "mock", "mock-1", 2, 20)
        diagnostic = run_worker("diagnostic", diagnostic_dir, "mock", "mock-1", 2, 20)

        self.assertTrue(DIAGNOSTIC_REPAIR.startswith(GENERIC_REPAIR))
        self.assertFalse(generic["first_pass"])
        self.assertFalse(diagnostic["first_pass"])
        self.assertEqual(generic["status"], "failed_acceptance")
        self.assertEqual(len(generic["attempts"]), 3)
        self.assertEqual(diagnostic["status"], "success")
        self.assertEqual(len(diagnostic["attempts"]), 2)
        self.assertEqual(generic["attempts"][0]["verifier"],
                         diagnostic["attempts"][0]["verifier"])
        self.assertIn("quoted comma", diagnostic["attempts"][0]["verifier"])
        self.assertTrue(verify(diagnostic_dir / "workspace")[0])

    def test_each_seeded_bug_triggers_feedback_and_repairs_only_with_details(self):
        for bug_case in BUG_CASES:
            with self.subTest(bug_case=bug_case):
                seed_workspace = self.tmp / f"seed-{bug_case}"
                shutil.copytree(FIXTURE, seed_workspace)
                (seed_workspace / "csv_line.py").write_text(
                    seeded_source(bug_case), encoding="utf-8"
                )
                passed, detail = verify(seed_workspace)
                self.assertFalse(passed)
                self.assertIn(EXPECTED_FAILURE[bug_case], detail)

                generic_dir = self.tmp / f"generic-{bug_case}"
                diagnostic_dir = self.tmp / f"diagnostic-{bug_case}"
                generic_dir.mkdir()
                diagnostic_dir.mkdir()
                generic = run_worker(
                    "generic", generic_dir, "mock", "mock-1", 2, 20,
                    scenario="seeded-repair", bug_case=bug_case,
                )
                diagnostic = run_worker(
                    "diagnostic", diagnostic_dir, "mock", "mock-1", 2, 20,
                    scenario="seeded-repair", bug_case=bug_case,
                )
                self.assertEqual(generic["seed_verifier"], diagnostic["seed_verifier"])
                self.assertEqual(generic["status"], "failed_acceptance")
                self.assertEqual(len(generic["attempts"]), 3)
                self.assertEqual(diagnostic["status"], "success")
                self.assertEqual(len(diagnostic["attempts"]), 1)
