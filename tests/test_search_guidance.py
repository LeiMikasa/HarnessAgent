"""Search noise, invalid patterns and sandbox failures seen in real runs."""

import glob
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common import HarnessCase, make_settings
from agent.loop import execute_tool, run_loop
from agent.events import Hooks
from agent.llm import MockLLM
from agent.sandbox import DockerSandbox, SandboxUnavailable
from agent.tools.basic import register_basic_tools, run_bash, run_glob, run_grep
from agent.tools.feedback import ExecutionFeedback
from agent.tools.registry import ToolContext, ToolRegistry
from agent.tools.result import ToolResult
from agent.tools.search import IGNORED_DIRECTORIES


class SearchGuidanceTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(make_settings(self.tmp), self.tmp)

    def test_dependency_results_cannot_hide_source_and_are_not_traversed(self):
        self.write("web/src/app.ts", "needle")
        for index in range(205):
            self.write(f"web/node_modules/lib/{index}.ts", "needle")
        for name in IGNORED_DIRECTORIES:
            self.write(f"{name}/nested/ignored.ts", "needle")
        original = os.scandir
        scanned = []

        def scan(path):
            scanned.append(path)
            return original(path)

        with patch("agent.tools.search.os.scandir", side_effect=scan):
            self.assertEqual(run_glob({"pattern": "**/*.ts"}, self.ctx), "web/src/app.ts")
            self.assertEqual(run_grep({"pattern": "needle"}, self.ctx), "web/src/app.ts:1:needle")
        for path in scanned:
            self.assertFalse(set(path.relative_to(self.tmp).parts) & IGNORED_DIRECTORIES, path)

    def test_explicit_patterns_paths_and_opt_in_reach_ignored_directories(self):
        self.write("web/node_modules/lib/a.ts", "needle")
        self.write(".venv/lib/a.py", "needle")
        self.write(".git/config", "needle")
        for pattern, expected in (
            ("web/node_modules/**/*.ts", "web/node_modules/lib/a.ts"),
            ("**/.venv/**/*.py", ".venv/lib/a.py"),
            (".git/*", ".git/config"),
        ):
            with self.subTest(pattern=pattern):
                self.assertEqual(run_glob({"pattern": pattern}, self.ctx), expected)
        all_matches = run_glob({"pattern": "**/*", "include_ignored": True}, self.ctx)
        self.assertIn(".git/config", all_matches)
        self.assertIn("web/node_modules/lib/a.ts", all_matches)
        self.assertIn(".venv/lib/a.py:1:needle", run_grep({"pattern": "needle", "path": ".venv"}, self.ctx))
        self.assertIn("web/node_modules/lib/a.ts:1:needle", run_grep(
            {"pattern": "needle", "include": "web/node_modules/**/*.ts"}, self.ctx))
        self.assertIn(".git/config:1:needle", run_grep({"pattern": "needle", "include_ignored": True}, self.ctx))

    def test_glob_semantics_remain_compatible_for_source_patterns(self):
        for name in ("a.py", "src/a1.py", "src/nested/a2.py", "src/other.txt", ".hidden.py"):
            self.write(name, "source")
        patterns = ["*.py", "**/*.py", "src/*", "src/**", "src/**/a?.py", "**/src/**/*.py",
                    "src/*/", str(self.tmp / "src" / "*.py")]
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                expected = {Path(p).as_posix()
                            for p in glob.glob(pattern, root_dir=self.tmp, recursive=True)}
                actual = set(run_glob({"pattern": pattern}, self.ctx).splitlines())
                self.assertEqual(actual, expected)

    def test_brace_expansion_is_an_argument_error_not_a_false_no_match(self):
        for pattern in ("*.{ts,vue}", "file{1..3}.py"):
            result = run_glob({"pattern": pattern}, self.ctx)
            self.assertEqual((result.error_code, result.field, result.action),
                             ("INVALID_ARGUMENT", "pattern", "correct_arguments"))
        result = run_grep({"pattern": "x", "include": "*.{ts,vue}"}, self.ctx)
        self.assertEqual((result.error_code, result.field), ("INVALID_ARGUMENT", "include"))
        self.write("literal{a,b}.txt", "xxx")
        self.assertIn("literal{a,b}.txt", run_glob({"pattern": "literal[{]a,b[}].txt"}, self.ctx))
        # Regex quantifiers are supported; only glob patterns reject expansion.
        self.assertIn("xxx", run_grep({"pattern": "x{2,3}", "path": "literal{a,b}.txt"}, self.ctx))

    def test_registered_tools_accept_opt_in_but_retain_path_boundaries(self):
        self.write("node_modules/a.py", "needle")
        registry = register_basic_tools(ToolRegistry())
        for name, args in (("glob", {"pattern": "**/*.py", "include_ignored": True}),
                           ("grep", {"pattern": "needle", "include_ignored": True})):
            result = execute_tool({"name": name, "input": args}, registry=registry, ctx=self.ctx, hooks=Hooks())
            self.assertTrue(result.ok, result)
            self.assertIn("node_modules/a.py", result)
        with self.assertRaises(ValueError):
            run_glob({"pattern": "../*", "include_ignored": True}, self.ctx)

    def test_recursive_search_does_not_follow_outside_links_or_cycles(self):
        inside = self.write("src/a.py", "needle")
        outside = self.write("outside/secret.py", "needle")
        root = self.tmp / "src"
        try:
            (root / "outside").symlink_to(outside.parent, target_is_directory=True)
            (root / "cycle").symlink_to(root, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable on this host")
        ctx = ToolContext(make_settings(root), root)
        result = run_glob({"pattern": "**/*.py", "include_ignored": True}, ctx)
        self.assertIn(inside.name, result)
        self.assertNotIn("secret", result)
        self.assertLess(len(result.splitlines()), 4)


class SandboxFailureGuidanceTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.box = DockerSandbox(str(self.tmp))
        self.runtime = SimpleNamespace(sandbox_enabled=True, sandbox=None, permission_mode="workspace")
        self.runtime.get_sandbox = Mock(side_effect=lambda _: self.box.start() or self.box)
        self.ctx = ToolContext(make_settings(self.tmp), self.tmp, runtime=self.runtime)

    def test_start_failures_are_typed_and_do_not_fall_back_to_host(self):
        failures = [subprocess.CompletedProcess([], 125, "", "Docker engine unavailable"),
                    FileNotFoundError("docker"), subprocess.TimeoutExpired("docker run", 60)]
        for failure in failures:
            with self.subTest(failure=failure), patch("agent.tools.basic.subprocess.run") as host:
                self.box._docker = Mock(**({"side_effect": failure} if isinstance(failure, Exception)
                                          else {"return_value": failure}))
                result = run_bash({"command": "echo alive"}, self.ctx)
                self.assertEqual(result.error_code, "SANDBOX_UNAVAILABLE")
                self.assertEqual(result.execution_status, "not_executed")
                self.assertEqual(result.action, "restore_environment")
                self.assertFalse(result.transient)
                self.assertIn("file tools remain available", result)
                self.assertIn("restore_environment", result.model_content())
                host.assert_not_called()

    def test_command_failures_and_unknown_side_effects_keep_their_meaning(self):
        self.box.started = True
        self.box._docker = Mock(return_value=subprocess.CompletedProcess([], 125, "", "Cannot connect to the Docker daemon"))
        result = run_bash({"command": "some-program"}, self.ctx)
        self.assertEqual((result.error_code, result.execution_status), ("COMMAND_FAILED", "unknown"))
        self.box.execute = Mock(side_effect=TimeoutError("execution timed out"))
        result = run_bash({"command": "some-program"}, self.ctx)
        self.assertEqual((result.error_code, result.execution_status), ("TIMEOUT", "unknown"))

    def test_failure_reminder_survives_file_reads_and_changes_in_command(self):
        feedback = ExecutionFeedback()
        registry = register_basic_tools(ToolRegistry())
        failure = ToolResult.from_exception(SandboxUnavailable("offline"))
        feedback.observe("bash", {"command": "git status"}, failure, registry.get("bash"), ctx=self.ctx)
        feedback.observe("read_file", {"path": "a.py"}, ToolResult("source"), registry.get("read_file"), ctx=self.ctx)
        self.assertIn("[sandbox unavailable]", feedback.render(self.ctx))
        feedback.observe("bash", {"command": "echo alive"}, failure, registry.get("bash"), ctx=self.ctx)
        self.assertIn("[sandbox unavailable]", feedback.render(self.ctx))
        feedback.observe("bash", {"command": "echo recovered"}, ToolResult("ok"), registry.get("bash"), ctx=self.ctx)
        self.assertNotIn("[sandbox unavailable]", feedback.render(self.ctx))
        feedback.observe("bash", {"command": "git status"}, failure, registry.get("bash"), ctx=self.ctx)
        self.ctx.runtime.permission_mode = "full"
        self.assertNotIn("[sandbox unavailable]", feedback.render(self.ctx))
        self.ctx.runtime.permission_mode = "workspace"
        feedback.observe("bash", {"command": "git status"}, failure, registry.get("bash"), ctx=self.ctx)
        feedback.observe("bash", {"command": "git status"},
                         ToolResult.failure("COMMAND_FAILED", "check failed", execution_status="unknown"),
                         registry.get("bash"), ctx=self.ctx)
        self.assertNotIn("[sandbox unavailable]", feedback.render(self.ctx))

    def test_loop_reports_environment_failure_without_automatic_retries(self):
        self.box._docker = Mock(return_value=subprocess.CompletedProcess([], 125, "", "engine offline"))
        llm = MockLLM(script=[{"tool": "bash", "input": {"command": "git status"}}, "limited overview"])
        requests = []
        original = llm.create

        def create(**kwargs):
            requests.append(kwargs["system"])
            return original(**kwargs)

        llm.create = create
        result = run_loop(llm=llm, registry=register_basic_tools(ToolRegistry()), system="sys", ctx=self.ctx,
                          messages=[{"role": "user", "content": "inspect"}], max_turns=3)
        self.assertTrue(result.ok)
        self.assertEqual(self.box._docker.call_count, 1)
        self.assertIn("[sandbox unavailable]", requests[1])
