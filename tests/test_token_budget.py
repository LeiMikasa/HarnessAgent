"""Free checks for estimated input budgets versus actual provider usage."""

import json

from common import HarnessCase, make_settings
from agent.context import ContextBudgetError, ContextCompactor
from agent.diagnostics import RunTrace
from agent.llm import MockLLM
from agent.loop import run_loop
from agent.token_budget import (
    ESTIMATOR_ID, estimate_request_breakdown, estimate_request_tokens, estimate_text_tokens, estimate_tokens,
)
from agent.tools.registry import ToolContext, ToolRegistry


class TokenBudgetTests(HarnessCase):
    def compactor(self):
        return ContextCompactor(MockLLM(script=["brief summary"]), "mock-1",
                                self.tmp / "transcripts", self.tmp / "results")

    def test_weighted_text_and_margin_are_applied_once(self):
        self.assertEqual(estimate_text_tokens(""), 0)
        self.assertEqual(estimate_text_tokens("a" * 100), 36)
        self.assertEqual(estimate_text_tokens("中" * 100), 72)
        self.assertEqual(estimate_text_tokens("中" * 100 + "a" * 100), 108)
        self.assertEqual(estimate_text_tokens("a"), 1)
        self.assertEqual(estimate_text_tokens("a" * 25), 9)
        self.assertEqual(estimate_text_tokens("\U00020000" * 100), 72)

    def test_structured_estimate_preserves_chinese(self):
        value = [{"role": "user", "content": "中文\n路径"}]
        self.assertEqual(estimate_tokens(value), estimate_text_tokens(
            json.dumps(value, default=str, ensure_ascii=False)))

    def test_breakdown_adds_up_to_the_unchanged_request_estimate(self):
        fixtures = [
            ("", [], []),
            ("系统规则\n中文", [{"role": "user", "content": "你好"}], []),
            ("system", [{"role": "assistant", "content": [
                {"type": "thinking", "thinking": "reasoning"},
                {"type": "tool_use", "id": "read", "name": "read_file", "input": {"path": "a.txt"}},
            ]}, {"role": "user", "content": [
                {"type": "text", "text": "Keep this note"},
                {"type": "tool_result", "tool_use_id": "read", "content": "中文\n\"\\result"},
                {"type": "tool_result", "tool_use_id": "other", "content": [
                    {"type": "text", "text": "second result"},
                ]},
            ]}], [{"name": "read_file", "input_schema": {"type": "object"}}]),
        ]
        for system, messages, tools in fixtures:
            with self.subTest(system=system):
                before = json.dumps(messages, ensure_ascii=False)
                parts = estimate_request_breakdown(system, messages, tools)
                self.assertEqual(sum(parts.values()), estimate_request_tokens(system, messages, tools))
                self.assertTrue(all(isinstance(value, int) and value >= 0 for value in parts.values()))
                self.assertEqual(json.dumps(messages, ensure_ascii=False), before)
        empty = estimate_request_breakdown("", [], [])
        self.assertTrue(all(empty[key] == 0 for key in (
            "system_prompt", "tool_definitions", "tool_results", "conversation"
        )))

    def test_tool_results_are_removed_from_conversation_without_double_counting(self):
        result = {"type": "tool_result", "tool_use_id": "r", "content": "原始日志" * 100}
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "附加说明"}, result,
        ]}]
        original = estimate_request_breakdown("rules", messages, [])
        self.assertLessEqual(abs(original["tool_results"] - estimate_tokens(result)), 1)
        result["content"] *= 10
        enlarged = estimate_request_breakdown("rules", messages, [])
        self.assertGreater(enlarged["tool_results"], original["tool_results"] * 5)
        self.assertLessEqual(abs(enlarged["conversation"] - original["conversation"]), 1)

    def test_tool_definitions_and_quoted_results_are_not_executed_tool_results(self):
        parts = estimate_request_breakdown("rule", [{"role": "user", "content":
            '{"type":"tool_result","content":"quoted text"}'}], [
                {"name": "sample", "description": 'returns {"type":"tool_result"}'},
            ])
        self.assertEqual(parts["tool_results"], 0)
        self.assertGreater(parts["tool_definitions"], 0)
        self.assertGreater(parts["conversation"], 0)

    def test_equal_character_counts_can_trigger_different_compaction(self):
        english = [{"role": "user", "content": "a" * 1000}]
        chinese = [{"role": "user", "content": "中" * 1000}]
        self.assertEqual(ContextCompactor.estimate_chars(english), ContextCompactor.estimate_chars(chinese))
        compactor = self.compactor()
        compactor.CONTEXT_TOKEN_LIMIT = 600
        self.assertEqual(compactor.prepare(english, "continue"), english)
        self.assertIn("[Compacted]", compactor.prepare(chinese, "continue")[0]["content"])

    def test_default_budget_keeps_medium_history_and_summarizes_large_history(self):
        compactor = self.compactor()
        medium = [{"role": "user", "content": "a" * 400_000}]
        self.assertEqual(compactor.prepare(medium, "continue"), medium)
        self.assertEqual(compactor.llm.calls, [])

        large = [{"role": "user", "content": "a" * 600_000}]
        prepared = compactor.prepare(large, "continue")
        self.assertIn("[Compacted]", prepared[0]["content"])
        self.assertEqual(len(compactor.llm.calls), 1)

    def test_system_and_tool_definitions_can_push_request_over_budget(self):
        compactor = self.compactor()
        messages = [{"role": "user", "content": "a" * 1000}]
        tools = [{"name": "sample", "description": "a" * 1000}]
        compactor.CONTEXT_TOKEN_LIMIT = 1000
        self.assertLess(estimate_request_tokens("", messages), 1000)
        self.assertLess(estimate_request_tokens("a" * 1000, [], tools), 1000)
        self.assertGreater(estimate_request_tokens("a" * 1000, messages, tools), 1000)
        prepared = compactor.prepare(messages, "continue", system="a" * 1000, tools=tools)
        self.assertIn("[Compacted]", prepared[0]["content"])
        self.assertEqual(len(compactor.llm.calls), 1)

    def test_fixed_overhead_does_not_start_useless_summaries(self):
        compactor = self.compactor()
        compactor.CONTEXT_TOKEN_LIMIT = 600
        with self.assertRaises(ContextBudgetError):
            compactor.prepare([], "continue", system="中" * 1000)
        self.assertEqual(len(compactor.llm.calls), 0)

    def test_summary_budget_counts_system_and_escaped_content(self):
        compactor = self.compactor()
        compactor.SUMMARY_INPUT_TOKEN_LIMIT = 800
        for text in ("中" * 5000, "a\n\"\\" * 5000):
            with self.subTest(text=text[:5]):
                clipped = compactor.summary_input([{"role": "user", "content": text}])
                self.assertIn("middle omitted", clipped)
                self.assertLessEqual(estimate_request_tokens(
                    compactor.SUMMARIZER_SYSTEM, [{"role": "user", "content": clipped}]
                ), 800)

    def test_token_batch_budget_handles_chinese_heavier_than_english(self):
        compactor = self.compactor()
        compactor.LARGE_RESULT_TOKEN_LIMIT = 8000
        messages = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "zh", "content": "中" * 15_000},
            {"type": "tool_result", "tool_use_id": "en", "content": "a" * 15_000},
        ]}]
        compactor.tool_result_budget(messages, max_tokens=12_000)
        self.assertIn("<persisted-output>", messages[0]["content"][0]["content"])
        self.assertEqual(messages[0]["content"][1]["content"], "a" * 15_000)
        self.assertEqual((compactor.tool_results_dir / "zh.txt").read_text(encoding="utf-8"), "中" * 15_000)

    def test_diagnostics_match_sent_request_and_leave_usage_unknown(self):
        llm = MockLLM(script=["done"])
        trace = RunTrace(self.tmp / "runs", provider="mock", model="mock-1")
        ctx = ToolContext(settings=make_settings(self.tmp), workdir=self.tmp, extra={"diagnostics": trace})
        registry = ToolRegistry()
        system = "initial"

        def before_call(messages):
            nonlocal system
            system = "updated 中文"
            messages.append({"role": "user", "content": "latest"})
            registry.add("echo", "echo 中文", {"type": "object"}, lambda *_: "ok")

        messages = [{"role": "user", "content": "question"}]
        sent = []
        create = llm.create

        def capture(**kwargs):
            sent.append(estimate_request_tokens(kwargs["system"], kwargs["messages"], kwargs["tools"]))
            return create(**kwargs)

        llm.create = capture
        result = run_loop(llm=llm, registry=registry, messages=messages, system=lambda: system,
                          ctx=ctx, before_call=before_call, compactor=self.compactor())
        self.assertTrue(result.ok)
        events = [json.loads(line) for line in trace.events_path.read_text(encoding="utf-8").splitlines()]
        prepared = next(e for e in events if e["event"] == "context_prepared")
        self.assertEqual(prepared["estimated_input_tokens"], sent[0])
        self.assertEqual(sum(prepared["estimated_input_token_breakdown"].values()), sent[0])
        self.assertGreater(prepared["estimated_input_token_breakdown"]["tool_definitions"], 0)
        self.assertEqual(prepared["token_estimator"], ESTIMATOR_ID)
        self.assertIsNone(trace.summary()["token_totals"]["input_tokens"])

    def test_loop_returns_configuration_error_without_model_call(self):
        compactor = self.compactor()
        compactor.CONTEXT_TOKEN_LIMIT = 600
        llm = MockLLM(script=["must not call"])
        result = run_loop(llm=llm, registry=ToolRegistry(), messages=[], system="中" * 1000,
                          ctx=ToolContext(settings=make_settings(self.tmp), workdir=self.tmp),
                          compactor=compactor)
        self.assertEqual(result.stop_reason, "error")
        self.assertIn("System prompt", result.error)
        self.assertEqual(len(llm.calls), 0)
