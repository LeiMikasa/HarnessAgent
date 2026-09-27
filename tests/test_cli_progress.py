"""Human-facing progress must not change the agent loop's event semantics."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

from common import HarnessCase, make_settings

from agent.cli import main
from agent.llm import LLMResponse, MockLLM
from agent.loop import run_loop
from agent.tools.registry import ToolContext, ToolRegistry


class ProgressTests(HarnessCase):
    def test_progress_precedes_model_and_tools_without_exposing_payloads(self):
        progress: list[str] = []
        events: list[str] = []
        registry = ToolRegistry()

        def echo(args, _ctx):
            self.assertEqual(progress[-1], "[tool] echo: running")
            return f"secret-output-for-{args['text']}"

        registry.add("echo", "Echo text", {"type": "object", "properties": {}}, echo)

        def responder(index, _messages, _tools):
            self.assertEqual(progress[-1], f"[model] round {index + 1}: waiting for response")
            if index == 0:
                return [
                    {"tool": "echo", "input": {"text": "private-one"}},
                    {"tool": "echo", "input": {"text": "private-two"}},
                ]
            return "finished"

        result = run_loop(
            llm=MockLLM(responder=responder),
            registry=registry,
            messages=[{"role": "user", "content": "go"}],
            system="sys",
            ctx=ToolContext(settings=make_settings(self.tmp), workdir=self.tmp),
            on_event=events.append,
            on_progress=progress.append,
        )

        self.assertEqual(result.text, "finished")
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(sum(item == "[tool] echo: running" for item in progress), 2)
        self.assertEqual(sum(item.startswith("[tool] echo: returned") for item in progress), 2)
        self.assertNotIn("private-one", "\n".join(progress))
        self.assertNotIn("secret-output", "\n".join(progress))
        self.assertTrue(any("secret-output" in item for item in events))

    def test_provider_error_reports_progress(self):
        llm = MockLLM()

        def fail(**_kwargs):
            raise RuntimeError("provider unavailable")

        llm.create = fail  # type: ignore[assignment]
        progress: list[str] = []
        result = run_loop(
            llm=llm,
            registry=ToolRegistry(),
            messages=[{"role": "user", "content": "go"}],
            system="sys",
            ctx=ToolContext(settings=make_settings(self.tmp), workdir=self.tmp),
            on_progress=progress.append,
        )

        self.assertEqual(result.stop_reason, "error")
        self.assertEqual(progress[-1], "[model] round 1: request failed")

    def test_thinking_block_is_not_reported_as_progress(self):
        llm = MockLLM()
        llm.create = lambda **_kwargs: LLMResponse(  # type: ignore[assignment]
            content=[
                {"type": "thinking", "thinking": "private-reasoning"},
                {"type": "text", "text": "public answer"},
            ]
        )
        progress: list[str] = []
        result = run_loop(
            llm=llm,
            registry=ToolRegistry(),
            messages=[{"role": "user", "content": "go"}],
            system="sys",
            ctx=ToolContext(settings=make_settings(self.tmp), workdir=self.tmp),
            on_progress=progress.append,
        )

        self.assertEqual(result.text, "public answer")
        self.assertNotIn("private-reasoning", "\n".join(progress))

    def test_cli_default_progress_is_on_stderr_and_quiet_is_final_only(self):
        base = ["--mock", "--no-memory", "--no-teams", "--workdir", str(self.tmp)]

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main([*base, "hello"]), 0)
        self.assertEqual(stdout.getvalue(), "Done.\n")
        self.assertIn("[model] round 1: waiting for response", stderr.getvalue())

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main([*base, "--quiet", "hello"]), 0)
        self.assertEqual(stdout.getvalue(), "Done.\n")
        self.assertEqual(stderr.getvalue(), "")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main([*base, "--verbose", "hello"]), 0)
        self.assertIn("runtime ready:", stdout.getvalue())
        self.assertIn("Done.\n", stdout.getvalue())
        self.assertIn("[model] round 1: waiting for response", stderr.getvalue())
