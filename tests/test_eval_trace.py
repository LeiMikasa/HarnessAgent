"""Offline payload recording and non-forcing completion reminders."""

import json
from pathlib import Path
from unittest.mock import patch

from common import HarnessCase, make_settings
from agent.events import Hooks, PRE_TOOL_USE
from agent.llm import LLMResponse, MockLLM
from agent.loop import execute_tool, run_loop
from agent.prompt import build_system_prompt
from agent.tools.registry import ToolContext, ToolRegistry
from agent.tools.result import tool_error
from evals.local_trace import EvaluationTrace
from evals.recall50 import CheckpointLLM, run_worker
from evals.realistic_cases import materialize, grade
from evals.recall_real import select_specs


class EvaluationTraceTests(HarnessCase):
    def test_requests_are_saved_before_history_mutation_without_clients_or_callbacks(self):
        trace = EvaluationTrace(self.tmp / "trace", secrets=("fixture-secret",))
        llm = CheckpointLLM(MockLLM(script=["fixture-secret ok"]), self.tmp / "telemetry.json",
                            payload_trace=trace)
        messages = [{"role": "user", "content": "original request"}]
        llm.create(system="fixture-secret rules", messages=messages, tools=[], max_tokens=32,
                   on_text=lambda _: None)
        messages[0]["content"] = "changed after request"
        folder = trace.directory / "model-calls/call-001"
        request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(request["messages"][0]["content"], "original request")
        self.assertNotIn("on_text", request)
        self.assertEqual(request["model"], "mock-1")
        self.assertIn("[REDACTED]", request["system"])
        for path in trace.directory.rglob("*"):
            if path.is_file():
                self.assertNotIn("fixture-secret", path.read_text(encoding="utf-8"))
        self.assertIn("ok", (folder / "response.json").read_text())

    def test_provider_error_is_saved_and_propagated_without_fake_success(self):
        class FailingClient:
            provider = "fixture"
            model = "fixture"
            def create(self, **kwargs):
                raise RuntimeError("fixture-secret unavailable")
        trace = EvaluationTrace(self.tmp / "trace", secrets=("fixture-secret",))
        llm = CheckpointLLM(FailingClient(), self.tmp / "telemetry.json", payload_trace=trace)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            llm.create(system="sys", messages=[], tools=[], max_tokens=32)
        error = json.loads((trace.directory / "model-calls/call-001/error.json").read_text())
        self.assertEqual(error["error_type"], "RuntimeError")
        self.assertEqual(error["message"], "[REDACTED] unavailable")
        self.assertFalse((trace.directory / "model-calls/call-001/response.json").exists())

    def test_logging_io_failure_does_not_replace_model_response(self):
        self.write("blocked", "not a directory")
        trace = EvaluationTrace(self.tmp / "blocked" / "trace")
        llm = CheckpointLLM(MockLLM(script=["ok"]), self.tmp / "telemetry.json", payload_trace=trace)
        result = llm.create(system="sys", messages=[], tools=[], max_tokens=32)
        self.assertEqual(result.text(), "ok")
        self.assertTrue(trace.errors)

    def test_permission_and_validation_failures_are_logged_without_execution(self):
        trace = EvaluationTrace(self.tmp / "trace")
        ctx = ToolContext(make_settings(self.tmp), self.tmp)
        ctx.extra["observe_tool_result"] = trace.tool_result
        registry = ToolRegistry()
        calls = []
        registry.add("probe", "", {"type": "object"}, lambda *_: calls.append(True) or "ok")
        hooks = Hooks()
        hooks.register(PRE_TOOL_USE, lambda *_: tool_error("PERMISSION_DENIED", "denied"))
        execute_tool({"id": "a", "name": "probe", "input": {}}, registry, ctx, hooks)
        execute_tool({"id": "b", "name": "missing", "input": {}}, registry, ctx, hooks)
        rows = [json.loads(line) for line in (trace.directory / "tools.jsonl").read_text().splitlines()]
        self.assertEqual([r["error_code"] for r in rows], ["PERMISSION_DENIED", "UNKNOWN_TOOL"])
        self.assertEqual(calls, [])

    def test_tool_output_is_complete_even_when_timeline_preview_is_shortened(self):
        trace = EvaluationTrace(self.tmp / "trace")
        from agent.tools.result import ToolResult
        trace.tool_result({"id": "x", "name": "read_file", "input": {"path": "source"}}, ToolResult("x" * 12000))
        row = json.loads((trace.directory / "tools.jsonl").read_text())
        self.assertEqual(row["output"], "x" * 12000)
        self.assertLess((trace.directory / "timeline.md").stat().st_size, 4000)

    def test_real_worker_records_both_requests_and_final_tools_outside_workspace(self):
        spec = select_specs(ids="live-files-orders")[0]
        run_dir = self.tmp / "worker"
        run_dir.mkdir()
        result = run_worker(spec, "compact", run_dir, "mock", "recall50-smoke", 10, 4000,
                            case=materialize(spec), grader=grade)
        self.assertTrue(result["success"])
        trace = Path(result["local_trace_dir"])
        workspace = Path(result["workspace"])
        self.assertFalse(trace.is_relative_to(workspace))
        rows = [json.loads(line) for line in (trace / "tools.jsonl").read_text().splitlines()]
        self.assertIn("write_file", [r["tool"] for r in rows])
        self.assertTrue(list((trace / "model-calls").glob("*/request.json")))
        self.assertTrue(list((trace / "model-calls").glob("*/response.json")))
        self.assertEqual(result["local_trace_errors"], [])

    def test_grader_only_fields_are_never_added_to_model_requests(self):
        spec = select_specs()[0]
        case = materialize(spec)
        case["expected"]["hidden_grader_sentinel"] = "NEVER_INJECT_THIS_VALUE"
        run_dir = self.tmp / "no-leak"
        run_dir.mkdir()
        with patch("evals.recall50.SuiteMockLLM", return_value=MockLLM(script=["done"])):
            result = run_worker(spec, "full", run_dir, "mock", "recall50-smoke", 10, 4000,
                                case=case, grader=grade)
        request = next((Path(result["local_trace_dir"]) / "model-calls").glob("*/request.json"))
        self.assertNotIn("NEVER_INJECT_THIS_VALUE", request.read_text())
        self.assertNotIn("hidden_grader_sentinel", request.read_text())


class CompletionReminderTests(HarnessCase):
    def test_budget_reminder_appears_before_last_three_rounds_without_forcing_completion(self):
        registry = ToolRegistry()
        registry.add("read_file", "", {"type": "object"}, lambda *_: "evidence", read_only=True)
        llm = MockLLM(script=[{"tool": "read_file", "input": {}}] * 4 + ["complete"])
        requests = []
        create = llm.create
        def capture(**kwargs):
            requests.append(kwargs["system"])
            return create(**kwargs)
        llm.create = capture
        result = run_loop(llm=llm, registry=registry, messages=[{"role": "user", "content": "inspect"}],
                          system="sys", ctx=ToolContext(make_settings(self.tmp), self.tmp), max_turns=6)
        self.assertTrue(result.ok)
        self.assertEqual(result.tool_calls, 4)
        self.assertNotIn("[turn budget]", requests[2])
        self.assertIn("3 model rounds remain", requests[3])
        self.assertIn("do not guess", requests[3])

    def test_history_guidance_is_general_and_contains_no_case_answers(self):
        prompt = build_system_prompt(workdir=self.tmp, tool_names=["glob", "read_file", "write_file"])
        self.assertIn("first use evidence already", prompt)
        self.assertIn("actual archive markers", prompt)
        self.assertIn("specific remaining gap", prompt)
        self.assertNotIn("music-984", prompt)
        self.assertNotIn("answer.json", prompt)

    def test_broken_observer_cannot_change_tool_outcome(self):
        ctx = ToolContext(make_settings(self.tmp), self.tmp)
        def broken(*_):
            raise RuntimeError("logging failure")
        ctx.extra["observe_tool_result"] = broken
        registry = ToolRegistry()
        registry.add("probe", "", {"type": "object"}, lambda *_: "done")
        result = execute_tool({"name": "probe", "input": {}}, registry, ctx, Hooks())
        self.assertTrue(result.ok)
        self.assertEqual(result, "done")
