"""Headless keyboard, approval and layout tests with an offline model."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from textual.containers import VerticalScroll
from textual.events import Paste
from textual.widgets import Static

from common import HarnessCase, make_settings
from agent.permissions import Rule
from agent.tui import ApprovalScreen, HarnessApp, MessagePane, Prompt
from agent.ui_events import UIEvent
from agent.token_budget import estimate_request_breakdown, estimate_request_tokens
from agent.tools.result import ToolResult


class TerminalTests(HarnessCase, unittest.IsolatedAsyncioTestCase):
    async def until(self, pilot, predicate, timeout=5):
        end = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > end:
                self.fail("UI condition did not become ready")
            await pilot.pause(0.03)

    async def test_multiline_submission_streams_once_and_history_restores(self):
        runtime = self.make_runtime(script=["第一段回答。第二段回答。"], teams_enabled=False)
        app = HarnessApp(runtime)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one(Prompt)
            prompt.insert("第一行")
            await pilot.press("ctrl+j")
            prompt.insert("第二行")
            self.assertEqual(prompt.text, "第一行\n第二行")
            await pilot.press("enter")
            await self.until(pilot, lambda: len(runtime.llm.calls) == 1 and not app.busy)
            texts = [pane.text for pane in app.query(MessagePane)]
            self.assertEqual(texts.count("第一段回答。第二段回答。"), 1)
            self.assertEqual(runtime.active_request, "第一行\n第二行")
            await pilot.press("ctrl+p")
            self.assertEqual(prompt.text, "第一行\n第二行")
            await pilot.press("ctrl+n")
            self.assertEqual(prompt.text, "")

    async def test_sending_from_history_scrolls_to_latest_response(self):
        runtime = self.make_runtime(script=["最新回答\n\n" * 40], teams_enabled=False)
        app = HarnessApp(runtime)
        async with app.run_test(size=(100, 30)) as pilot:
            chat = app.query_one("#chat", VerticalScroll)
            await app.add_message("历史", "过去的内容\n\n" * 40)
            await self.until(pilot, lambda: chat.max_scroll_y > 0 and chat.is_vertical_scroll_end)
            chat.scroll_to(y=0, animate=False, immediate=True)
            await pilot.pause()
            self.assertFalse(chat.is_vertical_scroll_end)
            app.query_one(Prompt).load_text("继续任务")
            await pilot.press("enter")
            await self.until(pilot, lambda: len(runtime.llm.calls) == 1 and not app.busy)
            await self.until(pilot, lambda: chat.is_vertical_scroll_end)
            self.assertEqual(app.model_panes[next(iter(app.model_panes))].text, "最新回答\n\n" * 40)

    async def test_stream_follows_layout_growth_but_preserves_manual_scroll(self):
        runtime = self.make_runtime(teams_enabled=False)
        app = HarnessApp(runtime)
        async with app.run_test(size=(100, 30)) as pilot:
            chat = app.query_one("#chat", VerticalScroll)
            chunk = "这是一段正在生成的回答。\n\n" * 30
            app.receive_event(UIEvent("text_delta", "lead", "scroll-test", chunk))
            await self.until(pilot, lambda: chat.max_scroll_y > 0 and chat.is_vertical_scroll_end)
            height = chat.max_scroll_y
            app.receive_event(UIEvent("text_delta", "lead", "scroll-test", chunk))
            await self.until(pilot, lambda: chat.max_scroll_y > height and chat.is_vertical_scroll_end)
            chat.scroll_to(y=0, animate=False, immediate=True)
            await pilot.pause()
            height, position = chat.max_scroll_y, chat.scroll_y
            app.receive_event(UIEvent("text_delta", "lead", "scroll-test", chunk))
            await self.until(pilot, lambda: chat.max_scroll_y > height)
            self.assertEqual(chat.scroll_y, position)
            self.assertFalse(chat.is_vertical_scroll_end)
            # Returning to the bottom enables follow again.
            chat.scroll_end(animate=False)
            await self.until(pilot, lambda: chat.is_vertical_scroll_end)
            height = chat.max_scroll_y
            app.receive_event(UIEvent("text_delta", "lead", "scroll-test", chunk))
            await self.until(pilot, lambda: chat.max_scroll_y > height and chat.is_vertical_scroll_end)

    async def test_tool_card_can_expand_and_owner_streams_stay_separate(self):
        self.write("file.txt", "local output")
        runtime = self.make_runtime(script=[{"tool": "read_file", "input": {"path": "file.txt"}}, "done"], teams_enabled=False)
        app = HarnessApp(runtime, initial_request="read")
        async with app.run_test(size=(120, 40)) as pilot:
            await self.until(pilot, lambda: not app.busy and len(app.tools) == 1)
            card, body, _ = next(iter(app.tools.values()))
            self.assertTrue(card.collapsed)
            # Model completion is reported before Markdown's deferred layout.
            # Wait for a stable, visible click target after following to bottom.
            card.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click(card, offset=(3, 0))
            self.assertFalse(card.collapsed)
            app.receive_event(UIEvent("text_delta", "worker", "worker-id", "队友内容"))
            app.receive_event(UIEvent("model_end", "worker", "worker-id", "队友内容", {"tool_calls": 0}))
            await self.until(pilot, lambda: "worker-id" in app.model_panes)
            self.assertEqual(app.model_panes["worker-id"].text, "队友内容")
            self.assertEqual(app.last_final, "done")
            self.assertGreater(app.context_usage["estimated_input_token_breakdown"]["tool_results"], 0)

    async def approval_case(self, allow):
        runtime = self.make_runtime(script=[{"tool": "guarded", "input": {}}, "done"], teams_enabled=False)
        runtime.permissions.approval = "ask"
        runtime.permissions.rules.insert(0, Rule("guard", ["guarded"], lambda n, a: True, "测试审批"))
        executed = []
        runtime.tools.add("guarded", "", {}, lambda a, c: executed.append(True) or "ok")
        app = HarnessApp(runtime, initial_request="run")
        with patch("builtins.input", side_effect=AssertionError("UI must not read terminal input")):
            async with app.run_test(size=(120, 40)) as pilot:
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                self.assertEqual(executed, [])
                await pilot.press("y" if allow else "n")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(executed, [True] if allow else [])

    async def test_approval_can_deny_without_running_tool(self):
        await self.approval_case(False)

    async def test_approval_can_allow_exactly_once(self):
        await self.approval_case(True)

    async def host_approval_case(self, allow):
        workspace = self.tmp / "project"
        workspace.mkdir()
        outside = self.write("outside.txt", "外部资料")
        runtime = self.make_runtime(
            settings=make_settings(workspace), permission_mode="workspace", teams_enabled=False,
            script=[{"tool": "read_file", "input": {"path": str(outside)}},
                    {"tool": "read_file", "input": {"path": "inside.txt"}}, "done"],
        )
        # Even automatic ordinary approval must not skip a host confirmation.
        self.assertEqual(runtime.permissions.approval, "allow")
        box = Mock()
        box.read_file.return_value = ToolResult("容器内资料")
        runtime.get_sandbox = Mock(return_value=box)
        app = HarnessApp(runtime, initial_request="读取资料")
        async with app.run_test(size=(120, 40)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn("本次完全权限", str(app.screen.query_one("#approval-heading", Static).render()))
            self.assertEqual(json.loads(app.current_approval.arguments)["path"], str(outside))
            await self.until(pilot, lambda: app.focused is not None)
            self.assertEqual(app.focused.id, "deny")
            runtime.get_sandbox.assert_not_called()
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            for button_id in ("deny", "allow"):
                button = app.screen.query_one(f"#{button_id}")
                self.assertLessEqual(button.region.bottom, app.size.height)
                self.assertTrue(button.visible)
            await pilot.press("y" if allow else "escape")
            await self.until(pilot, lambda: not app.busy)
            results = self.tool_result_texts(runtime)
            self.assertIn("外部资料" if allow else "PERMISSION_DENIED", results[0])
            self.assertIn("容器内资料", results[1])
            box.read_file.assert_called_once_with({"path": "inside.txt"})
            self.assertNotIn("host_execution_approved", runtime.lead_ctx.extra)
            self.assertIn("工作区内修改", str(app.query_one("#features", Static).render()))

    async def test_host_approval_is_explicit_under_auto_allow_and_returns_to_container(self):
        await self.host_approval_case(True)

    async def test_host_approval_escape_denies_and_returns_to_container(self):
        await self.host_approval_case(False)

    async def test_cancel_releases_permission_wait_and_prevents_tool_execution(self):
        runtime = self.make_runtime(script=[{"tool": "guarded", "input": {}}], teams_enabled=False)
        runtime.permissions.approval = "ask"
        runtime.permissions.rules.insert(0, Rule("guard", ["guarded"], lambda n, a: True, "confirm"))
        executed = []
        runtime.tools.add("guarded", "", {}, lambda a, c: executed.append(True))
        app = HarnessApp(runtime, initial_request="run")
        async with app.run_test(size=(120, 40)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.press("ctrl+c")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(executed, [])
            self.assertEqual(len(runtime.llm.calls), 1)
            self.assertFalse(isinstance(app.screen, ApprovalScreen))

    async def test_sidebar_hides_on_narrow_terminal_and_commands_are_free(self):
        runtime = self.make_runtime(teams_enabled=False)
        app = HarnessApp(runtime)
        async with app.run_test(size=(70, 30)) as pilot:
            self.assertFalse(app.query_one("#sidebar").display)
            await app.submit(":status")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(runtime.llm.calls, [])
            self.assertTrue(app.query_one(Prompt).is_on_screen)

    async def test_context_budget_displays_remaining_and_ignores_teammates(self):
        runtime = self.make_runtime(teams_enabled=False)
        app = HarnessApp(runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            usage = app.query_one("#context-usage", Static)
            compact_usage = app.query_one("#context-compact", Static)
            self.assertIn("等待首轮请求", str(usage.render()))
            app.receive_event(UIEvent("context_usage", data={
                "estimated_input_tokens": 45_000, "input_token_limit": 200_000,
                "estimated_input_token_breakdown": {
                    "system_prompt": 2000, "tool_definitions": 3000, "tool_results": 30_000,
                    "conversation": 9990, "request_structure": 10,
                },
            }))
            await self.until(pilot, lambda: app.context_usage is not None)
            text = str(usage.render())
            for value in ("最近请求", "45,000", "200,000", "155,000", "22.5%"):
                self.assertIn(value, text)
            for value in ("提示词  ≈2,000", "工具定义  ≈3,000", "工具结果  ≈30,000",
                          "对话消息  ≈9,990", "请求结构  ≈10"):
                self.assertIn(value, text)
            app.receive_event(UIEvent("context_usage", "worker", data={
                "estimated_input_tokens": 100, "input_token_limit": 1000,
            }))
            await app.drain_events()
            self.assertEqual(app.context_usage["estimated_input_tokens"], 45_000)
            await pilot.resize_terminal(60, 18)
            await pilot.pause()
            self.assertTrue(compact_usage.is_on_screen)
            self.assertFalse(usage.is_on_screen)
            self.assertTrue(app.query_one(Prompt).is_on_screen)
            self.assertLessEqual(compact_usage.region.bottom, app.query_one(Prompt).region.y)
            await pilot.resize_terminal(100, 24)
            await pilot.pause()
            self.assertTrue(usage.is_on_screen)
            self.assertEqual(usage.region.bottom, app.query_one("#sidebar").region.bottom)
            self.assertGreaterEqual(usage.region.x, app.query_one(Prompt).region.right)
            await app.submit(":clear")
            self.assertIsNone(app.context_usage)
            self.assertIn("等待首轮请求", str(usage.render()))
            self.assertEqual(runtime.llm.calls, [])

    async def test_context_budget_handles_overflow_and_disabled_compaction(self):
        runtime = self.make_runtime(teams_enabled=False)
        app = HarnessApp(runtime)
        async with app.run_test(size=(120, 40)) as pilot:
            usage = app.query_one("#context-usage", Static)
            app.receive_event(UIEvent("context_usage", data={
                "estimated_input_tokens": 220_000, "input_token_limit": 200_000,
            }))
            await app.drain_events()
            self.assertIn("剩余  ≈0", str(usage.render()))
            self.assertIn("110.0%", str(usage.render()))
            self.assertIn("超出预算 20,000", str(usage.render()))
            self.assertTrue(usage.has_class("over-budget"))
            await pilot.pause()
            self.assertGreaterEqual(usage.region.x, app.query_one(Prompt).region.right)
            self.assertEqual(usage.region.bottom, app.query_one("#sidebar").region.bottom)
            app.receive_event(UIEvent("context_usage", data={
                "estimated_input_tokens": 220_000, "input_token_limit": None,
            }))
            await app.drain_events()
            self.assertIn("自动压缩已关闭", str(usage.render()))
            self.assertNotIn("剩余", str(usage.render()))
            self.assertFalse(usage.has_class("over-budget"))

    async def test_context_budget_uses_the_request_after_compaction(self):
        runtime = self.make_runtime(script=["brief summary", "done"], teams_enabled=False)
        runtime.compactor.CONTEXT_TOKEN_LIMIT = 15_000
        runtime.messages.append({"role": "user", "content": "a" * 50_000})
        app = HarnessApp(runtime, initial_request="finish")
        async with app.run_test(size=(120, 40)) as pilot:
            await self.until(pilot, lambda: not app.busy and len(runtime.llm.calls) == 2)
            call = runtime.llm.calls[-1]
            self.assertEqual(app.context_usage["estimated_input_tokens"], estimate_request_tokens(
                call["system"], call["messages"], call["tools"]
            ))
            self.assertEqual(app.context_usage["input_token_limit"], 15_000)
            self.assertEqual(app.context_usage["estimated_input_token_breakdown"], estimate_request_breakdown(
                call["system"], call["messages"], call["tools"]
            ))
            self.assertLess(app.context_usage["estimated_input_tokens"], 15_000)
            self.assertIn("[Compacted]", str(call["messages"]))

    async def test_theme_and_resize_preserve_unicode_draft_and_focus(self):
        runtime = self.make_runtime(teams_enabled=False)
        app = HarnessApp(runtime)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one(Prompt)
            draft = "中文任务 👩‍💻\nHello مرحبا e\u0301"
            app.post_message(Paste(draft))
            await pilot.pause()
            self.assertEqual(prompt.text, draft)
            await pilot.press("ctrl+t")
            self.assertEqual(app.theme, "harness-light")
            self.assertEqual(prompt.text, draft)
            self.assertIs(app.focused, prompt)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertFalse(app.query_one("#sidebar").display)
            self.assertTrue(prompt.is_on_screen)
            self.assertTrue(app.query_one("#hint").is_on_screen)
            await pilot.resize_terminal(40, 12)
            await pilot.pause()
            self.assertTrue(app.query_one("#minimum-size").is_on_screen)
            self.assertFalse(app.query_one("#body").display)
            self.assertFalse(app.query_one("#context-usage").is_on_screen)
            await pilot.resize_terminal(120, 40)
            await pilot.press("ctrl+t")
            self.assertEqual(app.theme, "harness-dark")
            self.assertTrue(app.query_one("#sidebar").display)
            self.assertTrue(prompt.is_on_screen)
            self.assertEqual(prompt.text, draft)
            self.assertEqual(runtime.llm.calls, [])

    async def test_background_approval_uses_same_queue_and_explicit_owner(self):
        runtime = self.make_runtime(teams_enabled=False)
        runtime.permissions.approval = "ask"
        app = HarnessApp(runtime)
        async with app.run_test(size=(120, 40)) as pilot:
            def request():
                args = {"command": "x" * 6100 + "end-of-command"}
                app.receive_event(UIEvent("tool_start", "panel-dev", "bg", "guarded",
                                          {"arguments": args, "workdir": str(self.tmp)}))
                return app.approve("guarded", args, "confirm")
            task = asyncio.create_task(asyncio.to_thread(request))
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertEqual(app.current_approval.owner, "panel-dev")
            self.assertEqual(app.current_approval.workdir, str(self.tmp))
            self.assertIn("end-of-command", app.current_approval.arguments)
            await pilot.press("n")
            self.assertFalse(await asyncio.wait_for(task, 2))

    async def test_quit_stops_idle_background_team_even_without_active_request(self):
        runtime = self.make_runtime()
        worker = SimpleNamespace(alive=True, state=SimpleNamespace(role="tester", status="idle", claimed_task=None, turns=0, error=""))
        worker.stop = lambda: setattr(worker, "alive", False)
        worker.join = lambda timeout=None: None
        runtime.teams.teammates["worker"] = worker
        app = HarnessApp(runtime)
        async with app.run_test(size=(120, 40)) as pilot:
            self.assertFalse(app.busy)
            await pilot.press("ctrl+q")
            self.assertFalse(worker.alive)
            self.assertTrue(app.closing)


class TerminalCliTests(HarnessCase):
    def test_tui_routes_to_ui_without_raw_progress_or_verbose_prints(self):
        from agent.cli import main
        with patch("agent.tui.run_tui", return_value=0) as run:
            self.assertEqual(main(["--mock", "--tui", "--verbose", "--workdir", str(self.tmp), "hello"]), 0)
            runtime = run.call_args.args[0]
            self.assertIsNone(runtime.on_progress)
            self.assertIsNone(runtime.on_event)
            self.assertFalse(runtime.verbose)
            self.assertEqual(run.call_args.kwargs["initial_request"], "hello")
