"""Incomplete model output must not be accepted as task completion."""

import io
from contextlib import redirect_stdout

from common import HarnessCase, make_settings
from agent.events import Hooks, STOP
from agent.llm import LLMResponse, MockLLM
from agent.loop import run_loop
from agent.tools.registry import ToolContext, ToolRegistry


class IncompleteResponseTests(HarnessCase):
    def run_responses(self, responses, *, max_turns=10, registry=None):
        llm = MockLLM()
        requests = []
        queue = iter(responses)

        def create(**kwargs):
            requests.append(kwargs)
            return next(queue)

        llm.create = create
        messages = [{"role": "user", "content": "Create and verify a game"}]
        stops = []
        hooks = Hooks()
        hooks.register(STOP, lambda _messages: stops.append(True))
        events = []
        result = run_loop(
            llm=llm, registry=registry or ToolRegistry(), messages=messages,
            system="sys", ctx=ToolContext(settings=make_settings(self.tmp), workdir=self.tmp),
            hooks=hooks, max_turns=max_turns, on_event=events.append, on_diagnostic=events.append,
        )
        return result, messages, requests, stops, events

    def test_thinking_only_recovers_without_running_stop_hooks(self):
        result, messages, requests, stops, events = self.run_responses([
            LLMResponse([{"type": "thinking", "thinking": "private thought"}], "end_turn"),
            LLMResponse([{"type": "text", "text": "Verified result"}], "end_turn"),
        ])
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Verified result")
        self.assertEqual(len(stops), 1)
        self.assertEqual(len(requests), 2)
        self.assertIn("harness recovery", requests[1]["system"])
        self.assertNotIn("private thought", str(messages))
        self.assertNotIn("private thought", str(events))
        self.assertIn("text_chars=0", events[0])

    def test_repeated_empty_output_is_an_explicit_error(self):
        result, messages, requests, stops, _ = self.run_responses([LLMResponse([], "end_turn")] * 3)
        self.assertEqual(result.stop_reason, "error")
        self.assertIn("not confirmed complete", result.error)
        self.assertEqual(len(requests), 3)
        self.assertEqual(stops, [])
        self.assertEqual(len(messages), 1)

    def test_output_limit_text_continues_instead_of_being_final(self):
        result, messages, _, stops, _ = self.run_responses([
            LLMResponse([{"type": "text", "text": "Partial answer"}], "max_tokens"),
            LLMResponse([{"type": "text", "text": "Complete answer"}], "end_turn"),
        ])
        self.assertEqual(result.text, "Complete answer")
        self.assertEqual(len(stops), 1)
        self.assertIn("cut off", messages[2]["content"])

    def test_cut_off_tool_call_is_not_executed(self):
        executions = []
        registry = ToolRegistry()
        registry.add("write", "write", {"type": "object", "properties": {}}, lambda args, ctx: executions.append(args))
        response = LLMResponse([
            {"type": "tool_use", "id": "t1", "name": "write", "input": {}},
        ], "max_tokens")
        result, messages, requests, stops, _ = self.run_responses([response] * 3, registry=registry)
        self.assertFalse(result.ok)
        self.assertIn("not executed", result.error)
        self.assertIn("recovery exhausted", result.error)
        self.assertEqual(len(requests), 3)
        self.assertEqual(executions, [])
        self.assertEqual(len(messages), 1)
        self.assertEqual(stops, [])

    def test_cut_off_tool_call_recovers_using_only_complete_arguments(self):
        executions = []
        registry = ToolRegistry()
        registry.add("write", "write", {}, lambda args, ctx: executions.append(args) or "written")
        result, messages, requests, stops, _ = self.run_responses([
            LLMResponse([
                {"type": "thinking", "thinking": "discard this partial thought"},
                {"type": "tool_use", "id": "partial", "name": "write", "input": {"content": "partial"}},
            ], "max_tokens"),
            LLMResponse([
                {"type": "tool_use", "id": "complete", "name": "write", "input": {"content": "complete"}},
            ], "tool_use"),
            LLMResponse([{"type": "text", "text": "Verified"}], "end_turn"),
        ], registry=registry)
        self.assertTrue(result.ok)
        self.assertEqual(executions, [{"content": "complete"}])
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(len(stops), 1)
        self.assertIn("ONE small, complete tool call", requests[1]["system"])
        self.assertNotIn("harness recovery", requests[2]["system"])
        self.assertNotIn("discard this partial thought", str(messages))
        self.assertNotIn("'id': 'partial'", str(messages))
        self.assertIn("'tool_use_id': 'complete'", str(messages))

    def test_truncated_batch_executes_none_of_its_calls(self):
        executions = []
        registry = ToolRegistry()
        registry.add("write", "write", {}, lambda args, ctx: executions.append(args))
        result, messages, _, _, _ = self.run_responses([
            LLMResponse([
                {"type": "tool_use", "id": "first", "name": "write", "input": {"file": "a"}},
                {"type": "tool_use", "id": "second", "name": "write", "input": {"file": "b"}},
            ], "max_tokens"),
        ], registry=registry, max_turns=1)
        self.assertEqual(result.stop_reason, "max_turns")
        self.assertEqual(executions, [])
        self.assertEqual(len(messages), 1)

    def test_recovery_keeps_previously_executed_tools_without_repeating_them(self):
        executions = []
        registry = ToolRegistry()
        registry.add("write", "write", {}, lambda args, ctx: executions.append(args) or "written")
        result, messages, _, _, _ = self.run_responses([
            LLMResponse([{"type": "tool_use", "id": "old", "name": "write", "input": {"file": "a"}}], "tool_use"),
            LLMResponse([{"type": "tool_use", "id": "cut", "name": "write", "input": {"file": "b"}}], "max_tokens"),
            LLMResponse([{"type": "tool_use", "id": "new", "name": "write", "input": {"file": "b"}}], "tool_use"),
            LLMResponse([{"type": "text", "text": "Verified"}], "end_turn"),
        ], registry=registry)
        self.assertTrue(result.ok)
        self.assertEqual(executions, [{"file": "a"}, {"file": "b"}])
        self.assertEqual(result.tool_calls, 2)
        self.assertNotIn("'id': 'cut'", str(messages))

    def test_tool_and_empty_recovery_share_the_same_retry_budget(self):
        response = LLMResponse([{"type": "tool_use", "id": "cut", "name": "write", "input": {}}], "max_tokens")
        result, messages, requests, stops, _ = self.run_responses([
            LLMResponse([], "end_turn"), response, response,
        ])
        self.assertEqual(result.stop_reason, "error")
        self.assertIn("recovery exhausted", result.error)
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(messages), 1)
        self.assertEqual(stops, [])

    def test_recovery_obeys_overall_turn_limit(self):
        result, _, requests, stops, _ = self.run_responses([LLMResponse([], "max_tokens")], max_turns=1)
        self.assertEqual(result.stop_reason, "max_turns")
        self.assertEqual(len(requests), 1)
        self.assertEqual(stops, [])

    def test_verbose_log_labels_arguments_and_output_preview(self):
        events = []
        self.write("large.txt", "x" * 300)
        runtime = self.make_runtime(
            script=[{"tool": "read_file", "input": {"path": "large.txt", "offset": 1, "limit": 80}}, "done"],
            verbose=True, on_event=events.append,
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            runtime.submit("read")
        self.assertIn("offset=1", stdout.getvalue())
        self.assertIn("limit=80", stdout.getvalue())
        self.assertTrue(any("300 chars (showing first 160 chars)" in item for item in events))
        self.assertEqual(self.tool_result_texts(runtime), ["x" * 300])
