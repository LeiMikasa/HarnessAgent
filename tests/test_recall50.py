"""Independent recall grading, paired fairness, unknown tokens and resume."""

import argparse
import contextlib
import io
import json
import subprocess
from collections import Counter
from unittest.mock import patch

from common import HarnessCase
from evals.recall_cases import build_specs, grade_answer, load_specs, materialize
from evals.recall50 import SuiteMockLLM, aggregate, run_parent, run_worker, summarize


class Recall50Tests(HarnessCase):
    def write_answer(self, case, value):
        workspace = self.tmp / "answer-workspace"
        workspace.mkdir(exist_ok=True)
        (workspace / "answer.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return grade_answer(workspace, case)

    def test_dataset_has_50_conditions_and_300_known_targets(self):
        specs = load_specs()
        self.assertEqual(specs, build_specs())
        self.assertEqual(len({s["id"] for s in specs}), 50)
        self.assertEqual(set(Counter(s["family"] for s in specs).values()), {10})
        self.assertEqual(set(Counter(s["placement"] for s in specs).values()), {10})
        self.assertEqual(Counter(s["pressure"] for s in specs), {"medium": 25, "high": 25})
        cases = [materialize(s) for s in specs]
        self.assertEqual(sum(len(c["expected"]) for c in cases), 300)
        self.assertEqual(sum(len(c["absent_keys"]) for c in cases), 50)
        self.assertEqual(materialize(specs[0]), cases[0])

    def test_gold_matches_latest_approved_history_without_question_leakage(self):
        for spec in load_specs():
            with self.subTest(case_id=spec["id"]):
                case = materialize(spec)
                records = []
                for message in case["history"]:
                    content = message["content"]
                    strings = [content] if isinstance(content, str) else [b.get("content", "") for b in content]
                    for text in strings:
                        for line in text.splitlines():
                            marker = line.find("PROJECT_NOTE ")
                            if marker >= 0:
                                record, _ = json.JSONDecoder().raw_decode(line[marker + len("PROJECT_NOTE "):])
                                records.append(record)
                for key, value in case["expected"].items():
                    candidates = [r for r in records if r["project"] == case["project"] and r["key"] == key and r["approved"]]
                    self.assertEqual(max(candidates, key=lambda r: r["revision"])["value"], value)
                    if isinstance(value, str):
                        self.assertNotIn(value, case["prompt"])
                self.assertFalse(any(r["project"] == case["project"] and r["key"] in case["absent_keys"] for r in records))

    def test_grader_measures_partial_recall_and_abstention_separately(self):
        case = materialize(load_specs()[0])
        answer = {**case["expected"], "unassigned_contact": None}
        self.assertTrue(self.write_answer(case, answer)["passed"])
        answer["api_port"] = str(answer["api_port"])
        grade = self.write_answer(case, answer)
        self.assertEqual(grade["correct_items"], 5)
        self.assertEqual(grade["correct_abstentions"], 1)
        self.assertFalse(grade["passed"])
        answer["unassigned_contact"] = "invented-person"
        self.assertEqual(self.write_answer(case, answer)["correct_abstentions"], 0)

    def test_grader_rejects_bad_json_duplicates_and_nonstandard_constants(self):
        case = materialize(load_specs()[0])
        workspace = self.tmp / "invalid"
        workspace.mkdir()
        for raw in ('not json', '[]', '{"release_code":"a","release_code":"b"}', '{"api_port":NaN}'):
            (workspace / "answer.json").write_text(raw, encoding="utf-8")
            grade = grade_answer(workspace, case)
            self.assertEqual(grade["correct_items"], 0)
            self.assertTrue(grade["format_error"])
        self.assertEqual(grade_answer(self.tmp / "missing", case)["correct_items"], 0)

    def test_constraints_preserve_bool_and_list_order(self):
        case = materialize(next(s for s in load_specs() if s["family"] == "constraints"))
        answer = {**case["expected"], "unassigned_contact": None}
        answer["network_allowed"] = 0
        answer["allowed_formats"] = list(reversed(answer["allowed_formats"]))
        self.assertEqual(self.write_answer(case, answer)["correct_items"], 4)

    def test_timeout_and_failure_stay_in_denominator_and_hide_incomplete_tokens(self):
        record = {"expected_items": 6, "correct_items": 6, "abstention_targets": 1,
                  "correct_abstentions": 1, "success": True, "status": "success",
                  "elapsed_seconds": 1, "token_usage_complete": True,
                  "usage_calls": 2, "usage_missing_calls": 0,
                  "token_totals": {"input_tokens": 100, "output_tokens": 10}}
        failure = {**record, "correct_items": 0, "correct_abstentions": 0,
                   "status": "hard_timeout", "success": False, "token_usage_complete": False,
                   "token_totals": {"input_tokens": None, "output_tokens": None}}
        total = aggregate([record, failure])
        self.assertEqual(total["micro_recall"], 0.5)
        self.assertEqual(total["case_pass_rate"], 0.5)
        self.assertIsNone(total["token_totals"]["input_tokens"])

    def test_mock_paired_histories_tools_and_grader_are_isolated(self):
        records = []
        spec = next(s for s in load_specs() if s["family"] == "summary" and s["placement"] == "middle")
        for mode in ("full", "compact"):
            path = self.tmp / mode
            path.mkdir()
            result = run_worker(spec, mode, path, "mock", "mock-1", 12, 4000)
            result["repeat"] = 1
            self.assertTrue(result["success"], result)
            self.assertEqual(result["correct_items"], 6)
            self.assertTrue(result["tool_attempts"])
            self.assertTrue(all("error_code" in event and "arguments_sha256" in event
                                for event in result["tool_attempts"]))
            self.assertFalse(result["token_usage_complete"])
            self.assertIsNone(result["token_totals"]["input_tokens"])
            self.assertTrue((path / "case.json").is_file())
            self.assertFalse((path / "workspace" / "case.json").exists())
            records.append(result)
        self.assertTrue(records[1]["compaction_triggered"])
        self.assertGreater(records[1]["summary_calls"], 0)
        config = {"provider": "mock", "case_ids": [spec["id"]], "repeats": 1, "modes": ["full", "compact"]}
        summary = summarize(records, config)
        self.assertFalse(summary["is_model_quality_result"])
        self.assertEqual(summary["totals"]["compact"]["micro_recall"], 1)
        self.assertIsNone(summary["token_comparison_all_runs"]["token_reduction_percent"]["input_tokens"])
        records[1]["initial_history_sha256"] = "tampered"
        with self.assertRaises(RuntimeError):
            summarize(records, config)

    def test_resume_reuses_results_and_changed_budget_is_rejected(self):
        args = argparse.Namespace(family=None, ids="facts-start-medium", limit=None, provider="mock",
                                  model="mock-1", repeats=1, max_turns=12, max_tokens=4000,
                                  timeout=30, mode="both", plan=False, resume=None, output=self.tmp)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_parent(args), 0)
        batch = next(self.tmp.glob("recall50-*"))
        args.resume = batch
        with patch("evals.recall50.subprocess.run", side_effect=AssertionError("resume must not call models")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_parent(args), 0)
        args.max_tokens += 1
        with self.assertRaises(ValueError), contextlib.redirect_stdout(io.StringIO()):
            run_parent(args)

    def test_completed_artifact_is_not_credited_if_loop_exhausted_budget(self):
        spec = load_specs()[0]
        run_dir = self.tmp / "unfinished"
        run_dir.mkdir()
        result = run_worker(spec, "full", run_dir, "mock", "mock-1", 1, 4000)
        self.assertEqual(result["grading"]["correct_items"], 6)
        self.assertEqual(result["correct_items"], 0)
        self.assertFalse(result["success"])
        self.assertEqual(result["stop_reason"], "max_turns")

    def test_report_aggregates_known_usage_including_summary_calls(self):
        per_call_input = 100

        class UsageMock(SuiteMockLLM):
            def create(self, **kwargs):
                response = super().create(**kwargs)
                response.usage = {"input_tokens": per_call_input, "output_tokens": 20,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3}
                return response

        spec = next(s for s in load_specs() if s["family"] == "summary" and s["placement"] == "middle")
        records = []
        with patch("evals.recall50.SuiteMockLLM", UsageMock):
            for mode in ("full", "compact"):
                per_call_input = 100 if mode == "full" else 40
                run_dir = self.tmp / ("usage-" + mode)
                run_dir.mkdir()
                record = run_worker(spec, mode, run_dir, "mock", "mock-1", 12, 4000)
                record["repeat"] = 1
                self.assertTrue(record["token_usage_complete"])
                self.assertEqual(record["token_totals"]["input_tokens"], record["model_calls"] * per_call_input)
                telemetry = json.loads((run_dir / "telemetry.json").read_text(encoding="utf-8"))
                self.assertEqual(telemetry["token_totals"], record["token_totals"])
                records.append(record)
        self.assertGreater(records[1]["summary_calls"], 0)
        config = {"provider": "mock", "case_ids": [spec["id"]], "repeats": 1, "modes": ["full", "compact"]}
        summary = summarize(records, config)
        expected = round((1 - records[1]["model_calls"] * 40 / (records[0]["model_calls"] * 100)) * 100, 4)
        self.assertEqual(summary["token_comparison_all_runs"]["token_reduction_percent"]["input_tokens"], expected)
        self.assertEqual(summary["token_comparison_both_passed"]["token_reduction_percent"]["input_tokens"], expected)

    def test_parent_timeout_generates_zero_credit_and_unknown_token_report(self):
        args = argparse.Namespace(family=None, ids="facts-start-medium", limit=None, provider="mock",
                                  model="mock-1", repeats=1, max_turns=12, max_tokens=4000,
                                  timeout=30, mode="both", plan=False, resume=None, output=self.tmp)
        with patch("evals.recall50.subprocess.run", side_effect=subprocess.TimeoutExpired("fixture", 30)):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_parent(args), 1)
        summary_path = next(self.tmp.glob("recall50-*/summary.json"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        for mode in ("full", "compact"):
            self.assertEqual(summary["totals"][mode]["micro_recall"], 0)
            self.assertEqual(summary["totals"][mode]["expected_items"], 6)
            self.assertIsNone(summary["totals"][mode]["token_totals"]["input_tokens"])
        self.assertIsNone(summary["token_comparison_all_runs"]["token_reduction_percent"]["input_tokens"])
