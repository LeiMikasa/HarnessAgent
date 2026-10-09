"""Fault injection verifies recovery decisions and prevents repeated side effects."""

import json
from unittest.mock import patch

from common import HarnessCase, make_settings
from agent.events import Hooks, PRE_TOOL_USE
from agent.loop import execute_tool
from agent.mcp import InProcessMCPClient, MCPManager, StdioMCPClient, MCPError
from agent.tools.basic import register_basic_tools
from agent.tools.recovery import ToolRecovery
from agent.tools.registry import ToolContext, ToolRegistry
from agent.tools.result import ToolResult


class ToolRecoveryTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.registry = register_basic_tools(ToolRegistry())
        self.ctx = ToolContext(settings=make_settings(self.tmp), workdir=self.tmp)
        self.hooks = Hooks()
        self.delays = []
        self.recovery = ToolRecovery(sleep=self.delays.append, random_value=lambda: 0.5)

    def call(self, name, args):
        return execute_tool({"name": name, "input": args}, self.registry,
                            self.ctx, self.hooks, self.recovery)

    def test_invalid_arguments_never_reach_permission_or_handler(self):
        seen = []
        self.hooks.register(PRE_TOOL_USE, lambda *args: seen.append(True))
        cases = [({}, "path"), ({"path": "a.txt", "content": 123}, "content"),
                 ({"path": "a.txt", "content": "x", "typo": True}, "<root>")]
        for args, field in cases:
            with self.subTest(args=args):
                result = self.call("write_file", args)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "INVALID_ARGUMENT")
                self.assertEqual(result.field, field)
        self.assertEqual(seen, [])
        self.assertFalse((self.tmp / "a.txt").exists())

    def test_root_types_nonfinite_values_and_bounds_are_rejected(self):
        for args in ([], "oops", None, {"command": "echo x", "timeout": float("nan")},
                     {"command": "echo x", "timeout": 0}):
            with self.subTest(args=args):
                self.assertEqual(self.registry.validate("bash", args).error_code, "INVALID_ARGUMENT")
        self.assertEqual(self.registry.validate("read_file", {"path": "x", "offset": True}).error_code,
                         "INVALID_ARGUMENT")

    def test_nested_schema_and_local_refs_validate_without_network(self):
        self.registry.add("nested", "", {"type": "object", "$defs": {"count": {"type": "integer"}},
                          "properties": {"item": {"$ref": "#/$defs/count"}}, "required": ["item"]},
                          lambda args, ctx: "ok")
        self.assertEqual(self.call("nested", {"item": "1"}).field, "item")
        self.assertTrue(self.call("nested", {"item": 1}).ok)
        self.registry.add("remote", "", {"$ref": "https://example.invalid/schema"}, lambda a, c: "bad")
        self.assertEqual(self.call("remote", {}).error_code, "INVALID_SCHEMA")

    def test_safe_transient_read_retries_then_succeeds_with_permission_each_attempt(self):
        attempts, permissions = [], []
        def flaky(args, ctx):
            attempts.append(True)
            if len(attempts) < 3:
                raise ConnectionError("temporary interruption")
            return "recovered"
        self.registry.add("fetch", "", {}, flaky, read_only=True, retry_safe=True)
        self.hooks.register(PRE_TOOL_USE, lambda *args: permissions.append(True))
        result = self.call("fetch", {})
        self.assertEqual(result, "recovered")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(permissions), 3)
        self.assertEqual(self.delays, [0.125, 0.25])
        self.assertEqual([e["auto_retry"] for e in self.recovery.events], [True, True, False])

    def test_transient_failure_stops_after_three_executions(self):
        attempts = []
        def fail(args, ctx):
            attempts.append(True)
            raise TimeoutError("temporary")
        self.registry.add("fetch", "", {}, fail, read_only=True, retry_safe=True)
        result = self.call("fetch", {})
        self.assertEqual(result.error_code, "TIMEOUT")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(self.delays), 2)

    def test_read_only_without_host_opt_in_does_not_retry(self):
        attempts = []
        def fail(args, ctx):
            attempts.append(True)
            raise ConnectionError("temporary")
        self.registry.add("fetch", "", {}, fail, read_only=True)
        self.call("fetch", {})
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.delays, [])

    def test_write_side_effect_followed_by_timeout_is_not_repeated(self):
        attempts = []
        def write_then_timeout(args, ctx):
            attempts.append(True)
            (self.tmp / "written.txt").write_text("already done", encoding="utf-8")
            raise TimeoutError("response lost")
        # Even a mistaken retry_safe flag cannot enable automatic write retries.
        self.registry.add("write", "", {}, write_then_timeout, retry_safe=True)
        result = self.call("write", {})
        self.assertEqual(len(attempts), 1)
        self.assertEqual(result.execution_status, "unknown")
        self.assertEqual(result.action, "inspect_state")
        self.assertEqual((self.tmp / "written.txt").read_text(), "already done")

    def test_bash_timeout_is_unknown_and_never_retried(self):
        import subprocess
        with patch("agent.tools.basic.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 1)) as run:
            result = self.call("bash", {"command": "echo x", "timeout": 1})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result.error_code, "TIMEOUT")
        self.assertEqual(result.execution_status, "unknown")
        self.assertEqual(self.delays, [])

    def test_permission_denied_and_unknown_errors_never_retry(self):
        attempts = []
        self.registry.add("fetch", "", {}, lambda a, c: attempts.append(True), read_only=True, retry_safe=True)
        self.hooks.register(PRE_TOOL_USE, lambda *args: ToolResult.failure("PERMISSION_DENIED", "denied"))
        self.assertEqual(self.call("fetch", {}).error_code, "PERMISSION_DENIED")
        self.assertEqual(attempts, [])
        self.hooks = Hooks()
        def fail(a, c):
            attempts.append(True)
            raise ValueError("unexpected bug")
        self.registry.get("fetch").handler = fail
        self.assertEqual(self.call("fetch", {}).error_code, "TOOL_ERROR")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.delays, [])

    def test_invalid_tool_name_and_input_do_not_crash(self):
        output = execute_tool({"name": [], "input": [1]}, self.registry, self.ctx, self.hooks, self.recovery)
        self.assertFalse(output.ok)
        result = self.call("write_file", {"path": "a.txt", "content": object()})
        self.assertEqual(result.error_code, "INVALID_ARGUMENT")
        self.assertEqual(self.registry.dispatch([], {}, self.ctx).error_code, "INVALID_ARGUMENT")
        self.assertEqual(self.call("write_file", {1: "x", "path": "x"}).error_code, "INVALID_ARGUMENT")

    def test_unknown_file_write_blocks_further_mutations_until_complete_inspection(self):
        attempts = []
        def uncertain_write(args, ctx):
            attempts.append(True)
            (self.tmp / args["path"]).write_text("already written", encoding="utf-8")
            raise TimeoutError("lost acknowledgement")
        self.registry.get("write_file").handler = uncertain_write
        args = {"path": "a.txt", "content": "already written"}
        self.assertEqual(self.call("write_file", args).execution_status, "unknown")
        self.assertEqual(self.call("edit_file", {"path": "a.txt", "old_text": "already", "new_text": "again"}).error_code,
                         "STATE_CHECK_REQUIRED")
        self.assertEqual(self.call("write_file", args).error_code, "STATE_CHECK_REQUIRED")
        self.call("read_file", {"path": "a.txt", "limit": 1})  # complete one-line file
        self.assertEqual(self.recovery.uncertain_files, set())
        self.assertEqual(len(attempts), 1)

    def test_partial_read_cannot_clear_unknown_file_state(self):
        def fail(args, ctx):
            (self.tmp / "a.txt").write_text("one\ntwo", encoding="utf-8")
            raise TimeoutError("lost")
        self.registry.get("write_file").handler = fail
        args = {"path": "a.txt", "content": "one\ntwo"}
        self.call("write_file", args)
        self.call("read_file", {"path": "a.txt", "limit": 1})
        self.assertEqual(self.call("write_file", args).error_code, "STATE_CHECK_REQUIRED")
        self.call("read_file", {"path": "a.txt"})
        self.assertEqual(self.recovery.uncertain_files, set())

    def test_long_truncated_read_cannot_clear_unknown_file_state(self):
        from agent.tools.basic import MAX_TOOL_OUTPUT
        def fail(args, ctx):
            (self.tmp / "a.txt").write_text("x" * (MAX_TOOL_OUTPUT + 1), encoding="utf-8")
            raise TimeoutError("lost")
        self.registry.get("write_file").handler = fail
        args = {"path": "a.txt", "content": "x"}
        self.call("write_file", args)
        self.call("read_file", {"path": "a.txt"})
        self.assertEqual(self.call("write_file", args).error_code, "STATE_CHECK_REQUIRED")

    def test_confirmed_absence_resolves_unknown_write_but_permission_failure_does_not(self):
        def fail(args, ctx):
            raise TimeoutError("unknown whether file was created")
        self.registry.get("write_file").handler = fail
        args = {"path": "missing.txt", "content": "x"}
        self.call("write_file", args)
        with patch("agent.tools.basic.Path.stat", side_effect=PermissionError("cannot inspect")):
            self.assertEqual(self.call("read_file", {"path": "missing.txt"}).error_code, "PERMISSION_DENIED")
        self.assertEqual(self.call("write_file", args).error_code, "STATE_CHECK_REQUIRED")
        self.assertEqual(self.call("read_file", {"path": "missing.txt"}).error_code, "NOT_FOUND")
        self.assertEqual(self.recovery.uncertain_files, set())

    def test_precondition_failure_requires_inspection_and_exhausts_repairs(self):
        self.write("a.txt", "actual content")
        for index in range(3):
            result = self.call("edit_file", {"path": "a.txt", "old_text": f"wrong-{index}", "new_text": "bad"})
            self.assertEqual(result.error_code, "PRECONDITION_FAILED")
            self.assertEqual(result.action, "inspect")
        # Reading does not erase the failed edit's repair budget.
        self.assertTrue(self.call("read_file", {"path": "a.txt"}).ok)
        result = self.call("edit_file", {"path": "a.txt", "old_text": "actual content", "new_text": "changed"})
        self.assertEqual(result.error_code, "RECOVERY_EXHAUSTED")
        self.assertEqual((self.tmp / "a.txt").read_text(), "actual content")

    def test_successful_repair_clears_target_budget(self):
        self.write("a.txt", "actual")
        self.call("edit_file", {"path": "a.txt", "old_text": "wrong", "new_text": "new"})
        self.assertTrue(self.call("edit_file", {"path": "a.txt", "old_text": "actual", "new_text": "new"}).ok)
        self.assertEqual(self.recovery.failures, {})

    def test_loop_feedback_marks_error_and_allows_model_argument_repair(self):
        runtime = self.make_runtime(script=[
            {"tool": "write_file", "input": {"path": "a.txt", "content": 123}},
            {"tool": "write_file", "input": {"path": "a.txt", "content": "fixed"}}, "done",
        ])
        runtime.submit("write")
        results = [b for m in runtime.messages if isinstance(m.get("content"), list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        self.assertTrue(results[0]["is_error"])
        self.assertEqual(json.loads(results[0]["content"])["error_code"], "INVALID_ARGUMENT")
        self.assertNotIn("is_error", results[1])
        self.assertEqual((self.tmp / "a.txt").read_text(), "fixed")
        self.assertEqual([e["phase"] for e in runtime.lead_ctx.extra["tool_attempts"]], ["validation", "execution"])

    def test_repair_state_isolated_between_user_requests(self):
        bad = {"tool": "write_file", "input": {"path": "a.txt", "content": 123}}
        runtime = self.make_runtime(script=[bad, bad, bad, "failed", bad, "failed again"])
        runtime.submit("one")
        runtime.submit("two")
        result = json.loads(self.tool_result_texts(runtime)[-1])
        self.assertEqual(result["error_code"], "INVALID_ARGUMENT")

    def test_plain_text_beginning_with_error_is_not_guessed_as_failure(self):
        self.registry.add("read", "", {}, lambda a, c: "Error: a literal line in a log", read_only=True, retry_safe=True)
        self.assertTrue(self.call("read", {}).ok)
        self.assertEqual(self.delays, [])

    def test_mcp_retry_requires_host_allowlist_not_server_annotations(self):
        attempts = []
        client = InProcessMCPClient("remote")
        def fail(**args):
            attempts.append(True)
            raise TimeoutError("temporary interruption")
        client.register([{"name": "fetch", "inputSchema": {"type": "object"},
                          "annotations": {"readOnlyHint": True}}], {"fetch": fail})
        manager = MCPManager()
        manager.connect(client)
        tools, _ = manager.assemble_tool_pool()
        self.registry.merge(tools)
        self.call("mcp__remote__fetch", {})
        self.assertEqual(len(attempts), 1)
        manager.retry_safe_tools.add(("remote", "fetch"))
        tools, _ = manager.assemble_tool_pool()
        self.registry.merge(tools, replace=True)
        self.call("mcp__remote__fetch", {})
        self.assertEqual(len(attempts), 4)

    def test_mcp_protocol_iserror_is_preserved_and_not_retried(self):
        client = StdioMCPClient("fake", ["unused"])
        client._process = object()  # avoid launching a real service
        with patch.object(client, "_request", return_value={"isError": True, "content": [{"type": "text", "text": "business failure"}]}):
            result = client.call_tool("fetch", {})
        self.assertEqual(result.error_code, "REMOTE_TOOL_ERROR")
        self.assertFalse(result.transient)
        with patch.object(client, "_request", side_effect=MCPError("lost", code="TIMEOUT", transient=True)):
            self.assertTrue(client.call_tool("fetch", {}).transient)

    def test_outside_approval_is_cleared_after_execution(self):
        self.hooks.register(PRE_TOOL_USE, lambda b, c: c.extra.update(allow_outside=True))
        self.call("read_file", {"path": "missing"})
        self.assertNotIn("allow_outside", self.ctx.extra)
