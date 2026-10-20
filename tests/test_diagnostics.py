"""Free checks for trace accounting, isolation, and conservative repeat labels."""

from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from common import HarnessCase, make_settings

from agent.cli import main
from agent.context import ContextCompactor
from agent.diagnostics import RunTrace
from agent.events import Hooks
from agent.llm import LLMResponse, MockLLM
from agent.loop import run_loop
from agent.tools.basic import register_basic_tools
from agent.tools.registry import ToolContext, ToolRegistry


class DiagnosticTests(HarnessCase):
    def trace(self, root=None):
        return RunTrace(root or self.tmp / "runs", provider="mock", model="mock-1")

    def drive(self, llm, registry=None, trace=None, max_turns=8):
        trace = trace or self.trace()
        ctx = ToolContext(settings=make_settings(self.tmp), workdir=self.tmp,
                          extra={"diagnostics": trace})
        result = run_loop(llm=llm, registry=registry or ToolRegistry(),
                          messages=[{"role": "user", "content": "private-user-text"}],
                          system="private-system-text", ctx=ctx, max_turns=max_turns)
        return result, trace

    @staticmethod
    def events(trace):
        return [json.loads(line) for line in trace.events_path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def echo_registry(handler=None):
        registry = ToolRegistry()
        registry.add("echo", "Echo", {"type": "object", "properties": {"text": {"type": "string"}},
                                       "required": ["text"]},
                     handler or (lambda args, _ctx: args["text"]), read_only=True, retry_safe=True)
        return registry

    def test_batch_is_one_round_and_trace_omits_payloads(self):
        llm = MockLLM(script=[[
            {"tool": "echo", "input": {"text": "private-argument-one"}},
            {"tool": "echo", "input": {"text": "private-argument-two"}},
        ], "private-final-answer"])
        result, trace = self.drive(llm, self.echo_registry())
        self.assertTrue(result.ok)
        summary = trace.summary()
        self.assertEqual(summary["counts"]["loop_rounds"], 2)
        self.assertEqual(summary["counts"]["model_calls"], 2)
        self.assertEqual(summary["counts"]["tool_calls"], 2)
        ends = [event for event in self.events(trace) if event["event"] == "tool_end"]
        self.assertEqual([event["round"] for event in ends], [1, 1])
        payload = trace.events_path.read_text(encoding="utf-8") + trace.summary_path.read_text(encoding="utf-8")
        for secret in ("private-argument", "private-final-answer", "private-user-text", "private-system-text"):
            self.assertNotIn(secret, payload)
        self.assertIsNone(summary["token_totals"]["input_tokens"])

    def test_repeat_observation_does_not_block_and_mutation_resets_it(self):
        self.write("config.txt", "value=1")
        registry = register_basic_tools(ToolRegistry())
        script = [
            {"tool": "read_file", "input": {"path": "config.txt"}},
            {"tool": "read_file", "input": {"path": "config.txt"}},
            {"tool": "write_file", "input": {"path": "config.txt", "content": "value=1"}},
            {"tool": "read_file", "input": {"path": "config.txt"}},
            "done",
        ]
        result, trace = self.drive(MockLLM(script=script), registry)
        self.assertEqual(result.tool_calls, 4)
        reads = [e for e in self.events(trace) if e["event"] == "tool_end" and e["tool"] == "read_file"]
        self.assertEqual([e["same_read_result_repeat"] for e in reads], [False, True, False])

    def test_external_file_change_produces_a_new_read_result(self):
        path = self.write("config.txt", "old")

        def responder(index, _messages, _tools):
            if index == 1:
                path.write_text("new", encoding="utf-8")
            return {"tool": "read_file", "input": {"path": "config.txt"}} if index < 2 else "done"

        result, trace = self.drive(MockLLM(responder=responder), register_basic_tools(ToolRegistry()))
        self.assertTrue(result.ok)
        self.assertEqual(trace.summary()["counts"]["same_read_result_repeats"], 0)

    def test_tool_retries_are_not_counted_as_extra_model_rounds(self):
        attempts = []

        def flaky(args, _ctx):
            attempts.append(args)
            if len(attempts) < 3:
                raise TimeoutError("private-error-text")
            return "ok"

        with patch("agent.tools.recovery.time.sleep"):
            result, trace = self.drive(MockLLM(script=[{"tool": "echo", "input": {"text": "go"}}, "done"]),
                                       self.echo_registry(flaky))
        counts = trace.summary()["counts"]
        self.assertTrue(result.ok)
        self.assertEqual(counts["model_calls"], 2)
        self.assertEqual(counts["tool_calls"], 1)
        self.assertEqual(counts["execution_attempts"], 3)
        self.assertEqual(counts["auto_retry_events"], 2)
        self.assertNotIn("private-error-text", trace.events_path.read_text(encoding="utf-8"))

    def test_validation_and_permission_errors_are_both_recorded(self):
        runtime = self.make_runtime(script=[
            {"tool": "read_file", "input": {}},
            {"tool": "read_file", "input": {"path": "../outside.txt"}},
            "done",
        ], teams_enabled=False, approval="deny")
        runtime.submit("go")
        events = self.events(runtime.diagnostics)
        phases = [e["phase"] for e in events if e["event"] == "tool_attempt"]
        self.assertEqual(phases, ["validation", "permission"])
        self.assertEqual(runtime.diagnostics.summary()["counts"]["tool_errors"], 2)

    def test_provider_failure_is_recorded_without_error_body(self):
        llm = MockLLM()

        def fail(**_kwargs):
            raise RuntimeError("private-provider-key")

        llm.create = fail
        result, trace = self.drive(llm)
        self.assertEqual(result.stop_reason, "error")
        self.assertEqual(trace.summary()["counts"]["model_errors"], 1)
        self.assertEqual(trace.summary()["loop_stop_reasons"], {"error": 1})
        self.assertNotIn("private-provider-key", trace.events_path.read_text(encoding="utf-8"))

    def test_max_turns_and_repeats_are_observable_without_forced_early_stop(self):
        llm = MockLLM(responder=lambda *_args: {"tool": "echo", "input": {"text": "same"}})
        result, trace = self.drive(llm, self.echo_registry(), max_turns=3)
        self.assertEqual(result.stop_reason, "max_turns")
        self.assertEqual(result.tool_calls, 3)
        self.assertEqual(trace.summary()["counts"]["same_read_result_repeats"], 2)
        self.assertEqual(trace.summary()["loop_stop_reasons"], {"max_turns": 1})

    def test_truncated_tool_call_is_discarded_and_recovery_is_recorded(self):
        llm = MockLLM()
        replies = iter([
            LLMResponse(content=[{"type": "tool_use", "id": "cut", "name": "echo",
                                  "input": {"text": "discard"}}], stop_reason="max_tokens"),
            LLMResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
        ])
        llm.create = lambda **_kwargs: next(replies)
        result, trace = self.drive(llm, self.echo_registry())
        self.assertTrue(result.ok)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(trace.summary()["counts"]["recovery_events"], 1)
        self.assertEqual(trace.summary()["counts"]["model_calls"], 2)

    def test_nested_subagent_has_its_own_loop_and_parent_id(self):
        runtime = self.make_runtime(script=[
            {"tool": "task", "input": {"prompt": "inspect something"}},
            "child result", "done",
        ], teams_enabled=False)
        runtime.submit("delegate")
        starts = [e for e in self.events(runtime.diagnostics) if e["event"] == "loop_start"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[1]["parent_loop_id"], starts[0]["loop_id"])
        self.assertEqual(starts[1]["owner"], "lead/sub")
        self.assertEqual(runtime.diagnostics.summary()["counts"]["model_calls"], 3)
        owners = runtime.diagnostics.summary()["by_owner"]
        self.assertEqual(owners["lead"]["model_calls"], 2)
        self.assertEqual(owners["lead/sub"]["model_calls"], 1)

    def test_threads_keep_distinct_loop_ids_and_rounds(self):
        runtime = self.make_runtime(teams_enabled=False)
        trace = runtime.diagnostics

        def worker(index):
            ctx = ToolContext(settings=make_settings(self.tmp), workdir=self.tmp,
                              owner=f"worker-{index}")
            return runtime.loop_runner(
                llm=MockLLM(script=[{"tool": "echo", "input": {"text": str(index)}}, "done"]),
                registry=self.echo_registry(), messages=[{"role": "user", "content": "go"}],
                system="sys", ctx=ctx, hooks=Hooks(), max_turns=8)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker, range(4)))
        self.assertEqual(results, ["done"] * 4)
        events = self.events(trace)
        starts = [e for e in events if e["event"] == "loop_start"]
        self.assertEqual(len({e["loop_id"] for e in starts}), 4)
        owners = {e["loop_id"]: e["owner"] for e in starts}
        for event in events:
            self.assertEqual(event["owner"], owners[event["loop_id"]])
        self.assertEqual(trace.summary()["counts"]["model_calls"], 8)
        self.assertEqual(trace.summary()["counts"]["tool_calls"], 4)
        for owner in owners.values():
            self.assertEqual(trace.summary()["by_owner"][owner]["model_calls"], 2)

    def test_memory_selection_and_extraction_are_separate_from_agent_rounds(self):
        runtime = self.make_runtime(script=["[0]", "done", "[]"], memory=True, teams_enabled=False)
        runtime.memory.write_record("language", "user", "Chinese language", "Prefer Chinese answers.")
        runtime.submit("Chinese please")
        summary = runtime.diagnostics.summary()
        self.assertEqual(summary["counts"]["loop_rounds"], 1)
        self.assertEqual(summary["model_calls_by_kind"],
                         {"memory_selection": 1, "agent": 1, "memory_extraction": 1})

    def test_summary_calls_are_included(self):
        runtime = self.make_runtime(script=["brief summary", "done"], teams_enabled=False)
        runtime.messages.append({"role": "user", "content": "x" * 50_000})
        with patch.object(ContextCompactor, "CONTEXT_TOKEN_LIMIT", 15_000):
            runtime.submit("finish")
        self.assertEqual(runtime.diagnostics.summary()["model_calls_by_kind"], {"summary": 1, "agent": 1})

    def test_trace_io_failure_does_not_fail_the_agent(self):
        root = self.write("not-a-directory", "file")
        result, trace = self.drive(MockLLM(script=["done"]), trace=self.trace(root))
        self.assertTrue(result.ok)
        self.assertIn("Trace write failed", trace.error)

    def test_usage_keeps_unknown_counts_unknown(self):
        llm = MockLLM()
        replies = iter([
            LLMResponse(content=[{"type": "text", "text": "done"}],
                        usage={"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 0}),
            LLMResponse(content=[{"type": "text", "text": "done"}],
                        usage={"input_tokens": True, "output_tokens": 3}),
        ])
        llm.create = lambda **_kwargs: next(replies)
        _, trace = self.drive(llm)
        self.assertEqual(trace.summary()["token_totals"]["input_tokens"], 10)
        self.drive(llm, trace=trace)
        summary = trace.summary()
        self.assertIsNone(summary["token_totals"]["input_tokens"])
        self.assertEqual(summary["token_totals"]["output_tokens"], 5)
        self.assertIsNone(summary["token_totals"]["cache_read_input_tokens"])


class CliModeTests(HarnessCase):
    def test_cli_defaults_to_lightweight_and_supports_explicit_overrides(self):
        for flags, expected in [
            ([], "teams=off, memory=off"),
            (["--full"], "teams=on, memory=on"),
            (["--teams"], "teams=on, memory=off"),
            (["--memory"], "teams=off, memory=on"),
            (["--full", "--no-memory"], "teams=on, memory=off"),
            (["--full", "--no-teams"], "teams=off, memory=on"),
        ]:
            with self.subTest(flags=flags):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(main(["--mock", "--status", "--workdir", str(self.tmp), *flags]), 0)
                self.assertIn(expected, out.getvalue())

    def test_default_cli_makes_one_mock_call_and_writes_trace(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--mock", "--workdir", str(self.tmp), "hello"]), 0)
        paths = list((self.tmp / ".agent" / "runs").glob("*/summary.json"))
        self.assertEqual(len(paths), 1)
        summary = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(summary["counts"]["model_calls"], 1)
        self.assertEqual(summary["model_calls_by_kind"], {"agent": 1})

    def test_no_trace_does_not_create_trace_files(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--mock", "--no-trace", "--workdir", str(self.tmp), "hello"]), 0)
        self.assertFalse((self.tmp / ".agent" / "runs").exists())
