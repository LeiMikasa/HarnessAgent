"""Token accounting must survive normalization, summaries and export."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from anthropic.types import Usage
from common import HarnessCase
from agent.llm import LLMResponse, normalize_response
from evals.context_compaction import FixtureMockLLM, MeasuredLLM, run_worker
from evals.export_context_compaction import export
from evals.regrade_context_compaction import regrade
from evals.token_usage import aggregate_token_usage, summarize_calls, token_comparison


class TokenUsageTests(HarnessCase):
    def test_sdk_usage_keeps_cache_and_nested_details(self):
        usage = Usage(input_tokens=100, output_tokens=20,
                      cache_creation_input_tokens=30, cache_read_input_tokens=40,
                      cache_creation={"ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 0})
        response = normalize_response(SimpleNamespace(content="done", stop_reason="end_turn", usage=usage))
        self.assertEqual(response.text(), "done")
        self.assertEqual(response.usage["input_tokens"], 100)
        self.assertEqual(response.usage["cache_read_input_tokens"], 40)
        self.assertEqual(response.usage["cache_creation"]["ephemeral_5m_input_tokens"], 30)
        json.dumps(response.usage)

    def test_dict_usage_is_copied_with_provider_extras(self):
        raw = {"content": "done", "stop_reason": "end_turn", "usage": {
            "input_tokens": 0, "output_tokens": 2, "output_tokens_details": {"reasoning_tokens": 1}}}
        response = normalize_response(raw)
        raw["usage"]["output_tokens_details"]["reasoning_tokens"] = 999
        self.assertEqual(response.usage["output_tokens_details"]["reasoning_tokens"], 1)
        self.assertEqual(response.stop_reason, "end_turn")

    def test_missing_usage_and_legacy_results_are_unknown(self):
        self.assertIsNone(normalize_response(SimpleNamespace(content="done")).usage)
        self.assertIsNone(LLMResponse([], "end_turn").usage)
        summary = summarize_calls([{"usage": None}])
        self.assertFalse(summary["token_usage_complete"])
        self.assertEqual(summary["usage_missing_calls"], 1)
        self.assertTrue(all(value is None for value in summary["token_totals"].values()))
        self.assertIsNone(aggregate_token_usage([{}])["token_totals"]["input_tokens"])

    def test_zero_counts_are_valid_and_cache_remains_separate(self):
        summary = summarize_calls([{"usage": {"input_tokens": 0, "output_tokens": 0,
                                               "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40}}])
        self.assertTrue(summary["token_usage_complete"])
        self.assertEqual(summary["token_totals"]["input_tokens"], 0)
        self.assertEqual(summary["token_totals"]["cache_read_input_tokens"], 40)
        self.assertIsNone(token_comparison(summary, summary)["token_reduction_percent"]["input_tokens"])

    def test_incomplete_fields_never_produce_partial_totals(self):
        summary = summarize_calls([{"usage": {"input_tokens": 100, "output_tokens": 10}},
                                   {"usage": {"input_tokens": True, "output_tokens": -1}}])
        self.assertIsNone(summary["token_totals"]["input_tokens"])
        self.assertIsNone(summary["token_totals"]["output_tokens"])
        valid = summarize_calls([{"usage": {"input_tokens": 100, "output_tokens": 20}}])
        aggregate = aggregate_token_usage([valid, {}])
        self.assertFalse(aggregate["token_usage_complete"])
        self.assertIsNone(aggregate["token_totals"]["input_tokens"])
        self.assertIsNone(aggregate["usage_missing_calls"])

    def test_failed_request_counts_as_missing_usage(self):
        class FailingLLM:
            provider = model = "test"

            def create(self, **kwargs):
                raise RuntimeError("prompt_too_long")

        measured = MeasuredLLM(FailingLLM())
        with self.assertRaises(RuntimeError):
            measured.create(system="test", messages=[], tools=[])
        self.assertEqual(measured.calls, 1)
        self.assertEqual(measured.context_errors, 1)
        self.assertEqual(measured.usage_by_call[0]["kind"], "summary")
        self.assertEqual(summarize_calls(measured.usage_by_call)["usage_missing_calls"], 1)

    def test_summary_usage_survives_worker_export_and_regrade(self):
        class UsageMock(FixtureMockLLM):
            def create(self, **kwargs):
                response = super().create(**kwargs)
                response.usage = {"input_tokens": 100, "output_tokens": 20,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3}
                return response

        results = []
        with patch("evals.context_compaction.FixtureMockLLM", UsageMock):
            for mode in ("full", "compact"):
                run_dir = self.tmp / mode
                run_dir.mkdir()
                result = run_worker("summarization", mode, run_dir, "mock", "mock-1", 28, 5000, 1)
                result["repeat"] = 1
                self.assertTrue(result["success"], result)
                self.assertTrue(result["token_usage_complete"])
                self.assertEqual(result["token_totals"]["input_tokens"], result["model_calls"] * 100)
                self.assertEqual(result["token_totals"]["output_tokens"], result["model_calls"] * 20)
                self.assertEqual(len(result["usage_by_call"]), result["model_calls"])
                saved = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["token_totals"], result["token_totals"])
                results.append(result)
        self.assertGreater(results[1]["summary_calls"], 0)
        self.assertTrue(any(c["kind"] == "summary" and c["usage"] for c in results[1]["usage_by_call"]))
        source, destination = self.tmp / "summary.json", self.tmp / "export.json"
        base = {"provider": "mock", "model": "mock-1", "cases": ["summarization"],
                "repeats": 1, "pairs_of_history": 28, "chars_per_result": 5000,
                "measurement_note": "test", "aggregate": {}, "results": results}
        source.write_text(json.dumps(base), encoding="utf-8")
        export(source, destination)
        exported = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(exported["totals"]["compact"]["token_totals"], results[1]["token_totals"])
        self.assertEqual(exported["token_comparison"], token_comparison(results[0], results[1]))
        regraded = self.tmp / "regraded.json"
        regrade(source, regraded)
        corrected = json.loads(regraded.read_text(encoding="utf-8"))
        self.assertEqual(corrected["aggregate"]["summarization"]["compact"]["token_totals"], results[1]["token_totals"])
        # Old summaries still export, without inventing historical tokens.
        for result in results:
            for key in ("token_totals", "token_usage_complete", "usage_by_call", "usage_calls", "usage_missing_calls"):
                result.pop(key)
        source.write_text(json.dumps(base), encoding="utf-8")
        export(source, destination)
        legacy = json.loads(destination.read_text(encoding="utf-8"))
        self.assertIsNone(legacy["totals"]["full"]["token_totals"]["input_tokens"])
        self.assertIsNone(legacy["token_comparison"]["token_reduction_percent"]["input_tokens"])

    def test_token_reduction_allows_increases(self):
        full = summarize_calls([{"usage": {"input_tokens": 100, "output_tokens": 10}}])
        compact = summarize_calls([{"usage": {"input_tokens": 40, "output_tokens": 15}}])
        comparison = token_comparison(full, compact)
        self.assertEqual(comparison["token_reduction_percent"]["input_tokens"], 60)
        self.assertEqual(comparison["token_reduction_percent"]["output_tokens"], -50)
