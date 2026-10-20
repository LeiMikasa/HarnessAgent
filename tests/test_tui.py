"""Headless keyboard, approval and layout tests with an offline model."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from textual.containers import VerticalScroll
from textual.events import Paste

from common import HarnessCase
from agent.permissions import Rule
from agent.tui import ApprovalScreen, HarnessApp, MessagePane, Prompt
from agent.ui_events import UIEvent


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
