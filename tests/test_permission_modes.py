"""Permission boundaries and one-invocation host escalation, without model fees."""

import io
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common import HarnessCase, make_settings
from agent.cli import main
from agent.events import Hooks, PRE_TOOL_USE
from agent.loop import execute_tool
from agent.permissions import (
    ALLOW, DENY, FULL, READ_ONLY, WORKSPACE, CALL_PERMISSION_KEYS,
    HOST_EXECUTION_KEY, PermissionManager, Rule, _escapes_workspace, is_host_approval,
)
from agent.runtime import Runtime
from agent.sandbox import DockerSandbox
from agent.shell import noninteractive_process_options
from agent.tools.basic import register_basic_tools
from agent.tools.recovery import ToolRecovery
from agent.tools.registry import ToolContext, ToolRegistry
from agent.tools.result import ToolResult, tool_error


class PermissionModeTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.tmp / "project"
        self.workspace.mkdir()
        self.outside = self.tmp / "outside.txt"
        self.outside.write_text("port=8081", encoding="utf-8")
        self.registry = register_basic_tools(ToolRegistry(), sandbox_enabled=True)
        self.box = Mock()
        self.runtime = SimpleNamespace(sandbox_enabled=True, permission_mode=WORKSPACE,
                                       get_sandbox=Mock(return_value=self.box))
        self.ctx = ToolContext(make_settings(self.workspace), self.workspace, runtime=self.runtime)
        self.approver = Mock(return_value=True)
        self.manager = PermissionManager(workdir=self.workspace, sandbox_enabled=True,
                                         approver=self.approver, mode=WORKSPACE,
                                         tool_lookup=self.registry.get)
        self.hooks = Hooks()
        self.hooks.register(PRE_TOOL_USE, self.manager.check_hook)
        self.recovery = ToolRecovery(sleep=lambda _: None)

    def call(self, name, **args):
        result = execute_tool({"name": name, "input": args}, self.registry,
                              self.ctx, self.hooks, self.recovery)
        for key in CALL_PERMISSION_KEYS:
            self.assertNotIn(key, self.ctx.extra)
        return result

    def test_normal_file_call_uses_host_without_container_or_prompt(self):
        (self.workspace / "a.txt").write_text("host file", encoding="utf-8")
        self.assertEqual(self.call("read_file", path="a.txt"), "host file")
        self.approver.assert_not_called()
        self.runtime.get_sandbox.assert_not_called()

    def test_outside_read_approved_once_then_normal_call_keeps_workspace_boundary(self):
        self.assertEqual(self.call("read_file", path=str(self.outside)), "port=8081")
        self.runtime.get_sandbox.assert_not_called()
        self.assertTrue(is_host_approval(self.approver.call_args.args[2]))
        (self.workspace / "inside.txt").write_text("inside", encoding="utf-8")
        self.assertEqual(self.call("read_file", path="inside.txt"), "inside")
        self.assertEqual(self.approver.call_count, 1)
        self.assertEqual([event["execution_mode"] for event in self.recovery.events], ["host", "host"])
        self.runtime.get_sandbox.assert_not_called()

    def test_denied_outside_write_has_no_side_effect_and_is_asked_again(self):
        self.assertTrue(self.call("read_file", path=str(self.outside)).ok)
        self.approver.reset_mock()
        self.approver.side_effect = [False, True]
        args = {"path": str(self.outside), "content": "port=8082"}
        self.assertEqual(self.call("write_file", **args).error_code, "PERMISSION_DENIED")
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "port=8081")
        self.assertTrue(self.call("write_file", **args).ok)
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "port=8082")
        self.assertEqual(self.approver.call_count, 2)
        self.runtime.get_sandbox.assert_not_called()

    def test_approved_outside_edit_glob_and_grep(self):
        self.assertTrue(self.call("read_file", path=str(self.outside)).ok)
        self.assertTrue(self.call("edit_file", path=str(self.outside),
                                  old_text="8081", new_text="8082").ok)
        self.assertIn("outside.txt", self.call("glob", pattern=str(self.tmp / "*.txt")))
        self.assertIn("port=8082", self.call("grep", path=str(self.outside), pattern="port"))
        self.assertEqual(self.approver.call_count, 4)
        self.runtime.get_sandbox.assert_not_called()

    def test_explicit_host_command_needs_approval_and_next_command_uses_container(self):
        completed = subprocess.CompletedProcess([], 0, b"host", b"")
        with patch("agent.tools.basic.subprocess.run", return_value=completed) as run:
            self.assertEqual(self.call("bash", command="echo host", execution_mode="host",
                                       justification="Inspect an external directory"), "host")
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["cwd"], str(self.workspace))
        self.box.execute.return_value = subprocess.CompletedProcess([], 0, "container", "")
        self.assertEqual(self.call("bash", command="echo container"), "container")
        self.box.execute.assert_called_once()

    def test_real_host_echo_is_offline_and_does_not_start_docker(self):
        self.assertEqual(self.call("bash", command="echo permission-probe", execution_mode="host",
                                   justification="Verify one harmless host command"), "permission-probe")
        self.runtime.get_sandbox.assert_not_called()

    def test_host_flag_without_permission_hook_cannot_bypass_sandbox(self):
        result = self.registry.dispatch("read_file", {"path": str(self.outside),
                                        "execution_mode": "host", "justification": "test"}, self.ctx)
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.runtime.get_sandbox.assert_not_called()

    def test_invalid_host_request_is_rejected_before_approval(self):
        for args in ({"command": "echo x", "execution_mode": "host"},
                     {"command": "echo x", "execution_mode": "typo"}):
            self.assertEqual(self.call("bash", **args).error_code, "INVALID_ARGUMENT")
        self.approver.assert_not_called()

    def test_host_failure_does_not_retry_or_leak_permission(self):
        handler = Mock(return_value=tool_error("TIMEOUT", "temporary", transient=True))
        self.registry.get("read_file").handler = handler
        self.assertFalse(self.call("read_file", path=str(self.outside)).ok)
        self.assertEqual(handler.call_count, 1)
        self.assertFalse(self.recovery.events[-1]["auto_retry"])

    def test_handler_exception_clears_permission(self):
        self.registry.get("read_file").handler = Mock(side_effect=RuntimeError("fixture"))
        self.assertFalse(self.call("read_file", path=str(self.outside)).ok)

    def test_later_hook_veto_and_exception_clear_permission(self):
        self.hooks.register(PRE_TOOL_USE, lambda *args: ToolResult.failure("POLICY_BLOCKED", "blocked"))
        self.assertEqual(self.call("read_file", path=str(self.outside)).error_code, "POLICY_BLOCKED")
        self.hooks = Hooks()
        self.hooks.register(PRE_TOOL_USE, self.manager.check_hook)
        self.hooks.register(PRE_TOOL_USE, Mock(side_effect=RuntimeError("hook failed")))
        with self.assertRaises(RuntimeError):
            self.call("read_file", path=str(self.outside))
        self.assertFalse(any(key in self.ctx.extra for key in CALL_PERMISSION_KEYS))

    def test_child_context_never_inherits_temporary_permissions(self):
        self.ctx.extra.update({"allow_outside": True, HOST_EXECUTION_KEY: True, "ordinary": 1})
        child = self.ctx.child(owner="worker")
        self.assertEqual(child.extra, {"ordinary": 1})

    def test_approver_cannot_change_executed_arguments(self):
        self.assertTrue(self.call("read_file", path=str(self.outside)).ok)
        def approve(name, args, reason):
            args["content"] = "changed by callback"
            return True
        self.manager.approver = approve
        self.call("write_file", path=str(self.outside), content="original request")
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "original request")

    def test_auto_allow_does_not_authorize_host_execution_without_human(self):
        self.manager.approver = None
        self.manager.approval = ALLOW
        self.ctx.interactive = False
        self.assertEqual(self.call("read_file", path=str(self.outside)).error_code, "PERMISSION_DENIED")
        self.runtime.get_sandbox.assert_not_called()

    def test_deny_policy_blocks_even_with_approver(self):
        self.manager.approval = DENY
        self.assertEqual(self.call("read_file", path=str(self.outside)).error_code, "PERMISSION_DENIED")
        self.approver.assert_not_called()

    def test_deny_list_always_wins_even_in_full_mode(self):
        self.manager.mode = FULL
        self.assertEqual(self.call("bash", command="sudo echo fixture").error_code, "PERMISSION_DENIED")
        self.approver.assert_not_called()

    def test_custom_deny_rule_blocks_full_mode_and_explicit_escalation(self):
        self.manager.rules.insert(0, Rule("protected", ["read_file"], lambda n, a: True,
                                         "protected file", action=DENY))
        for mode in (WORKSPACE, FULL):
            self.manager.mode = mode
            self.assertEqual(self.call("read_file", path=str(self.outside), execution_mode="host",
                                       justification="fixture").error_code, "PERMISSION_DENIED")
        self.approver.assert_not_called()

    def test_read_only_blocks_ordinary_mutations_and_commands_but_allows_inspection(self):
        self.manager.mode = READ_ONLY
        for name, args in (("write_file", {"path": "x", "content": "x"}),
                           ("edit_file", {"path": "x", "old_text": "a", "new_text": "b"}),
                           ("bash", {"command": "echo x"})):
            self.assertEqual(self.call(name, **args).error_code, "PERMISSION_DENIED")
        (self.workspace / "x").write_text("inspection", encoding="utf-8")
        self.assertEqual(self.call("read_file", path="x"), "inspection")
        self.approver.assert_not_called()

    def test_read_only_can_request_one_explicit_host_mutation(self):
        self.manager.mode = READ_ONLY
        self.assertTrue(self.call("write_file", path="x", content="approved",
                                  execution_mode="host", justification="One requested change").ok)
        self.assertEqual((self.workspace / "x").read_text(encoding="utf-8"), "approved")
        self.assertEqual(self.call("write_file", path="x", content="denied").error_code, "PERMISSION_DENIED")

    def test_full_mode_uses_host_without_prompt_or_container(self):
        self.manager.mode = FULL
        self.runtime.permission_mode = FULL
        self.runtime.sandbox_enabled = False
        self.assertTrue(self.call("read_file", path=str(self.outside)).ok)
        self.assertTrue(self.call("write_file", path=str(self.outside), content="full").ok)
        self.assertEqual(self.call("read_file", path=str(self.outside)), "full")
        self.approver.assert_not_called()
        self.runtime.get_sandbox.assert_not_called()

    def test_container_error_never_falls_back_to_host(self):
        self.box.execute.side_effect = RuntimeError("Docker unavailable")
        with patch("agent.tools.basic.subprocess.run") as host_run:
            self.assertFalse(self.call("bash", command="echo probe").ok)
            host_run.assert_not_called()
        self.approver.assert_not_called()

    def test_container_absolute_workspace_path_does_not_request_host_access(self):
        (self.workspace / "nested").mkdir()
        (self.workspace / "nested/x").write_text("mapped file", encoding="utf-8")
        self.assertEqual(self.call("read_file", path="/workspace/nested/x"), "mapped file")
        self.approver.assert_not_called()
        self.runtime.get_sandbox.assert_not_called()

    def test_all_file_tools_use_host_and_share_workspace_aliases(self):
        self.assertTrue(self.call("write_file", path="/workspace/nested/config.txt", content="port=8080").ok)
        self.assertTrue(self.call("edit_file", path="nested/config.txt", old_text="8080", new_text="8081").ok)
        self.assertEqual(self.call("read_file", path=str(self.workspace / "nested/config.txt")), "port=8081")
        self.assertIn("config.txt", self.call("glob", pattern="/workspace/nested/*.txt"))
        self.assertIn("nested/config.txt:1:port=8081", self.call("grep", path="/workspace/nested", pattern="port"))
        self.assertEqual((self.workspace / "nested/config.txt").read_text(encoding="utf-8"), "port=8081")
        self.approver.assert_not_called()
        self.runtime.get_sandbox.assert_not_called()

    def test_read_only_file_handlers_reject_direct_mutations_without_hooks(self):
        self.runtime.permission_mode = READ_ONLY
        (self.workspace / "existing.txt").write_text("before", encoding="utf-8")
        for name, args in (("write_file", {"path": "new.txt", "content": "new"}),
                           ("edit_file", {"path": "existing.txt", "old_text": "before", "new_text": "after"})):
            result = self.registry.dispatch(name, args, self.ctx)
            self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertFalse((self.workspace / "new.txt").exists())
        self.assertEqual((self.workspace / "existing.txt").read_text(encoding="utf-8"), "before")
        self.assertTrue(self.registry.dispatch("read_file", {"path": "existing.txt"}, self.ctx).ok)

    def test_alias_traversal_is_not_allowed_to_hide_external_paths(self):
        self.approver.return_value = False
        for name, args in (("read_file", {"path": "/workspace/../outside.txt"}),
                           ("write_file", {"path": "/workspace/../outside.txt", "content": "bad"}),
                           ("glob", {"pattern": "/workspace/../*.txt"}),
                           ("grep", {"path": "/workspace/..", "pattern": "port"})):
            self.assertEqual(self.call(name, **args).error_code, "PERMISSION_DENIED")
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "port=8081")
        self.assertEqual(self.approver.call_count, 4)

    def test_file_handlers_keep_workspace_boundary_without_permission_hook(self):
        for name, args in (("read_file", {"path": str(self.outside)}),
                           ("write_file", {"path": str(self.outside), "content": "bad"}),
                           ("edit_file", {"path": str(self.outside), "old_text": "8081", "new_text": "bad"}),
                           ("glob", {"pattern": str(self.tmp / "*.txt")}),
                           ("grep", {"path": str(self.outside), "pattern": "port"})):
            self.assertEqual(self.registry.dispatch(name, args, self.ctx).error_code, "PERMISSION_DENIED")
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "port=8081")

    def test_aliases_share_unknown_write_state_until_complete_host_read(self):
        original_write = self.registry.get("write_file").handler
        def lose_acknowledgement(args, ctx):
            original_write(args, ctx)
            raise TimeoutError("lost acknowledgement")
        self.registry.get("write_file").handler = lose_acknowledgement
        self.assertEqual(self.call("write_file", path="/workspace/state.txt", content="before").execution_status, "unknown")
        self.assertEqual(self.call("edit_file", path="state.txt", old_text="before", new_text="after").error_code,
                         "STATE_CHECK_REQUIRED")
        self.assertTrue(self.call("read_file", path="/workspace/state.txt").ok)
        self.assertEqual(self.recovery.uncertain_files, set())
        self.assertTrue(self.call("edit_file", path=str(self.workspace / "state.txt"), old_text="before", new_text="after").ok)

    def test_symlink_target_outside_workspace_requires_approval(self):
        target = self.tmp / "outside-directory"
        target.mkdir()
        (target / "target.txt").write_text("private", encoding="utf-8")
        link = self.workspace / "outside-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            if os.name != "nt":
                self.skipTest(f"Symlinks unavailable: {exc}")
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, timeout=10, **noninteractive_process_options())
            if result.returncode:
                self.skipTest("Neither symlinks nor directory junctions are available")
        self.approver.return_value = False
        for name, args in (("read_file", {"path": "/workspace/outside-link/target.txt"}),
                           ("write_file", {"path": "/workspace/outside-link/target.txt", "content": "bad"}),
                           ("edit_file", {"path": "/workspace/outside-link/target.txt", "old_text": "private", "new_text": "bad"})):
            self.assertEqual(self.call(name, **args).error_code, "PERMISSION_DENIED")
            self.assertEqual(self.registry.dispatch(name, args, self.ctx).error_code, "PERMISSION_DENIED")
        self.assertEqual((target / "target.txt").read_text(encoding="utf-8"), "private")

    def test_permission_workspace_is_isolated_between_threads(self):
        other = self.tmp / "second"
        other.mkdir()
        barrier = threading.Barrier(2)
        def predicate(name, args):
            barrier.wait(timeout=5)
            return _escapes_workspace(name, args)
        manager = PermissionManager(workdir=self.workspace, approval=DENY,
                                    rules=[Rule("boundary", ["read_file"], predicate, "outside")])
        def check(root):
            return manager.check("read_file", {"path": str(root / "a")}, workdir=root).allowed
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(check, [self.workspace, other])), [True, True])

    def test_read_only_mount_is_enforced_by_docker(self):
        box = DockerSandbox(str(self.workspace), read_only=True)
        box._docker = Mock(return_value=subprocess.CompletedProcess([], 0, "fixture", ""))
        with patch("builtins.print"):
            box.start()
        args = box._docker.call_args.args[0]
        self.assertIn("target=/workspace,readonly", args[args.index("--mount") + 1])

    def test_runtime_modes_choose_execution_environment(self):
        for mode in (READ_ONLY, WORKSPACE, FULL):
            runtime = self.make_runtime(settings=make_settings(self.workspace), permission_mode=mode,
                                        teams_enabled=False)
            self.assertEqual(runtime.sandbox_enabled, mode != FULL)
            self.assertEqual(runtime.permissions.mode, mode)
        with self.assertRaises(ValueError):
            self.make_runtime(permission_mode=FULL, sandbox_enabled=True, teams_enabled=False)

    def test_cli_modes_default_and_incompatible_arguments(self):
        with patch.dict(os.environ, {"AGENT_PERMISSION_MODE": "workspace"}), redirect_stdout(io.StringIO()):
            with patch("agent.cli.Runtime.create") as create:
                runtime = create.return_value
                runtime.submit.return_value = "ok"
                self.assertEqual(main(["--mock", "--quiet", "--workdir", str(self.workspace), "hello"]), 0)
                self.assertEqual(create.call_args.kwargs["permission_mode"], WORKSPACE)
                self.assertEqual(main(["--mock", "--status", "--permission-mode", READ_ONLY]), 0)
                self.assertEqual(main(["--mock", "--sandbox", "--permission-mode", FULL]), 2)
                self.assertEqual(main(["--mock", "--teams", "--permission-mode", WORKSPACE]), 2)
