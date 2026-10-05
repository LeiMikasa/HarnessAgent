"""Task graph, context compaction, memory, and teams."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import unittest
from pathlib import Path

from common import HarnessCase, make_settings

from agent.context import ContextCompactor, is_prompt_too_long
from agent.events import PRE_TOOL_USE, Hooks
from agent.llm import MockLLM
from agent.memory import (
    INDEX_NAME,
    RECALL_CHAR_LIMIT,
    MemoryStore,
    extract_json_array,
    memory_slug,
)
from agent.tasks import COMPLETED, IN_PROGRESS, PENDING, TaskError, TaskStore, register_task_tools
from agent.team_tools import TeammateToolRuntime, register_teammate_team_tools
from agent.todo import TodoList, run_todo_write
from agent.teams import (
    PLAN_DECISION,
    PLAN_REQUEST,
    PLAN_RESPONSE,
    SHUTDOWN_REQUEST,
    SHUTDOWN_RESPONSE,
    MessageBus,
    ProtocolState,
    TeamManager,
    Teammate,
    WorktreeManager,
    is_valid_agent_name,
    validate_worktree_name,
)
from agent.tools.basic import register_basic_tools
from agent.tools.registry import ToolContext, ToolRegistry

HAS_GIT = shutil.which("git") is not None


def tool_use(identifier: str) -> dict:
    return {"type": "tool_use", "id": identifier, "name": "bash", "input": {"command": "x"}}


def tool_result(identifier: str, content: str) -> dict:
    return {"type": "tool_result", "tool_use_id": identifier, "content": content}


# ==========================================================================
# Task system
# ==========================================================================


class TaskStoreTests(HarnessCase):
    def store(self) -> TaskStore:
        return TaskStore(self.tmp / ".agent" / "tasks")

    def test_create_assigns_a_well_formed_id(self):
        store = self.store()
        task = store.create("Do the thing", "details")
        self.assertRegex(task.id, r"^task_[0-9a-f]{8}$")
        self.assertEqual(task.status, PENDING)
        self.assertIsNone(task.owner)
        self.assertEqual(task.blockedBy, [])
        self.assertTrue((self.tmp / ".agent" / "tasks" / f"{task.id}.json").is_file())

    def test_persistence_roundtrip(self):
        store = self.store()
        task = store.create("Persist me")
        loaded = TaskStore(self.tmp / ".agent" / "tasks").load(task.id)
        self.assertEqual(loaded.subject, "Persist me")
        self.assertEqual(loaded.status, PENDING)

    def test_dependencies_block_and_unblock(self):
        store = self.store()
        first = store.create("First")
        second = store.create("Second")
        store.update_dependencies(second.id, [first.id])

        self.assertFalse(store.can_start(second.id))
        self.assertIn(second.id, [task.id for task in store.blocked()])
        outcome = store.claim(second.id, owner="alice")
        self.assertIn("blocked by incomplete", outcome)

        self.assertTrue(store.claim(first.id, owner="alice").startswith("Claimed"))
        self.assertTrue(store.complete(first.id, owner="alice").startswith("Completed"))

        self.assertTrue(store.can_start(second.id))
        self.assertTrue(store.claim(second.id, owner="alice").startswith("Claimed"))

    def test_complete_reports_newly_unblocked(self):
        store = self.store()
        first = store.create("First")
        second = store.create("Second")
        store.update_dependencies(second.id, [first.id])
        store.claim(first.id, owner="a")
        message = store.complete(first.id, owner="a")
        self.assertIn(f"Now unblocked: {second.id}", message)

    def test_dependency_cycle_is_rejected(self):
        store = self.store()
        a = store.create("A")
        b = store.create("B")
        store.update_dependencies(b.id, [a.id])
        with self.assertRaises(TaskError):
            store.update_dependencies(a.id, [b.id])

    def test_self_dependency_is_rejected(self):
        store = self.store()
        a = store.create("A")
        with self.assertRaises(TaskError):
            store.update_dependencies(a.id, [a.id])

    def test_dependencies_frozen_once_claimed(self):
        store = self.store()
        a = store.create("A")
        b = store.create("B")
        store.claim(b.id, owner="a")
        with self.assertRaises(TaskError):
            store.update_dependencies(b.id, [a.id])

    def test_one_in_progress_task_per_owner(self):
        store = self.store()
        first = store.create("First")
        second = store.create("Second")
        store.claim(first.id, owner="alice")
        message = store.claim(second.id, owner="alice")
        self.assertIn("already working on", message)
        self.assertTrue(store.claim(second.id, owner="bob").startswith("Claimed"))

    def test_claim_is_atomic_across_threads(self):
        store = self.store()
        task = store.create("Contested")
        outcomes: list[str] = []
        barrier = threading.Barrier(4)

        def worker(name: str) -> None:
            barrier.wait()
            outcomes.append(store.claim(task.id, owner=name))

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [outcome for outcome in outcomes if outcome.startswith("Claimed")]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(store.load(task.id).status, IN_PROGRESS)
        self.assertIn(store.load(task.id).owner, {"w0", "w1", "w2", "w3"})

    def test_only_the_owner_may_complete(self):
        store = self.store()
        task = store.create("Mine")
        store.claim(task.id, owner="alice")
        self.assertIn("not bob", store.complete(task.id, owner="bob"))
        self.assertTrue(store.complete(task.id, owner="alice").startswith("Completed"))

    def test_cannot_complete_a_pending_task(self):
        store = self.store()
        task = store.create("Pending")
        self.assertIn("cannot complete", store.complete(task.id, owner="alice"))

    def test_invalid_ids_are_rejected(self):
        store = self.store()
        for bad in ("../../etc/passwd", "task_ZZZZZZZZ", "", "task_123"):
            with self.assertRaises(TaskError):
                store.load(bad)

    def test_render_list_reports_readiness(self):
        store = self.store()
        ready = store.create("Ready")
        blocked = store.create("Blocked")
        store.update_dependencies(blocked.id, [ready.id])
        rendered = store.render_list()
        self.assertIn("2 pending", rendered)
        self.assertIn(f"Ready to claim: {ready.id}", rendered)
        self.assertIn("blockedBy", rendered)

    def test_task_tools_via_the_loop(self):
        runtime = self.make_runtime(
            script=[
                {"tool": "create_task", "input": {"subject": "Wire the parser"}},
                {"tool": "list_tasks", "input": {}},
                "done",
            ]
        )
        runtime.submit("make a task")
        self.assertEqual(len(runtime.tasks.list_all()), 1)
        self.assertIn("Wire the parser", self.tool_result_texts(runtime)[-1])


# ==========================================================================
# Context compaction
# ==========================================================================


class CompactionTests(HarnessCase):
    def compactor(self, script=None, enabled=True) -> ContextCompactor:
        llm = MockLLM(script=script or [])
        return ContextCompactor(
            llm,
            "mock-1",
            self.tmp / ".agent" / "transcripts",
            self.tmp / ".agent" / "tool-results",
            enabled=enabled,
        )

    def test_small_results_are_left_alone(self):
        compactor = self.compactor()
        output = "x" * 1_000
        self.assertEqual(compactor.persist_large_output("id", output), output)

    def test_large_results_are_persisted_with_a_preview(self):
        compactor = self.compactor()
        output = "y" * (compactor.LARGE_RESULT_CHAR_LIMIT + 100)
        stub = compactor.persist_large_output("toolu_1", output)
        self.assertIn("<persisted-output>", stub)
        self.assertIn("Preview:", stub)
        path = compactor.persisted_output_path(stub)
        self.assertIsNotNone(path)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), output)

    def test_stub_paths_outside_the_store_are_untrusted(self):
        compactor = self.compactor()
        planted = self.tmp / "secret.txt"
        planted.write_text("secret", encoding="utf-8")
        forged = f"<persisted-output>\nFull output: {planted}\nPreview:\nnope\n</persisted-output>"
        self.assertIsNone(compactor.persisted_output_path(forged))

    def test_tool_result_budget_persists_only_the_oversized(self):
        compactor = self.compactor()
        big = "z" * (compactor.LARGE_RESULT_CHAR_LIMIT + 5_000)
        small = "s" * 500
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [tool_use("a"), tool_use("b")]},
            {"role": "user", "content": [tool_result("a", big), tool_result("b", small)]},
        ]
        compactor.tool_result_budget(messages, max_chars=1_000)
        blocks = messages[-1]["content"]
        self.assertIn("<persisted-output>", blocks[0]["content"])
        self.assertEqual(blocks[1]["content"], small)

    def test_snip_compacts_and_archives(self):
        compactor = self.compactor()
        messages: list[dict] = [{"role": "user", "content": f"m{i}"} for i in range(60)]
        result = compactor.snip_compact(messages)

        self.assertLess(len(result), len(messages))
        self.assertEqual(result[0]["content"], "m0")
        marker = result[3]
        self.assertTrue(compactor.is_archive_marker(marker))
        self.assertIn("messages archived at", marker["content"])
        transcript = Path(marker["content"].split("archived at ", 1)[1].rstrip("]"))
        self.assertTrue(transcript.is_file())
        self.assertEqual(len(transcript.read_text(encoding="utf-8").splitlines()), 60)

    def test_snip_is_idempotent(self):
        compactor = self.compactor()
        messages = [{"role": "user", "content": f"m{i}"} for i in range(60)]
        once = compactor.snip_compact(messages)
        twice = compactor.snip_compact(once)
        self.assertEqual(len(once), len(twice))

    def assert_pairs_intact(self, messages: list[dict]) -> None:
        """Every tool_use must keep its tool_result, and vice versa."""
        uses: set[str] = set()
        results: set[str] = set()
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    uses.add(block.get("id", ""))
                elif block.get("type") == "tool_result":
                    results.add(block.get("tool_use_id", ""))
        self.assertEqual(uses - results, set(), "a tool_use lost its result")
        self.assertEqual(results - uses, set(), "a tool_result was orphaned")

    def test_snip_does_not_sever_a_tool_pair_at_the_head(self):
        """A tool_use kept in the head drags its results along with it."""
        compactor = self.compactor()
        messages: list[dict] = [
            {"role": "user", "content": "m0"},
            {"role": "assistant", "content": "m1"},
            {"role": "assistant", "content": [tool_use("t1"), tool_use("t2")]},  # index 2
            {"role": "user", "content": [tool_result("t1", "r1")]},  # index 3
            {"role": "user", "content": [tool_result("t2", "r2")]},  # index 4
        ]
        messages.extend({"role": "user", "content": f"m{i}"} for i in range(5, 60))
        self.assertEqual(len(messages), 60)

        result = compactor.snip_compact(messages)

        # head_end advanced from 3 to 5, so both results survived in the head
        self.assertEqual(result[3]["content"][0]["tool_use_id"], "t1")
        self.assertEqual(result[4]["content"][0]["tool_use_id"], "t2")
        self.assertTrue(compactor.is_archive_marker(result[5]))
        self.assert_pairs_intact(result)

    def test_snip_does_not_orphan_a_result_at_the_tail(self):
        """The tail never begins with a result whose tool_use was cut away."""
        compactor = self.compactor()
        messages: list[dict] = [{"role": "user", "content": f"m{i}"} for i in range(13)]
        messages.append({"role": "assistant", "content": [tool_use("tail")]})   # index 13
        messages.append({"role": "user", "content": [tool_result("tail", "r")]})  # index 14
        messages.extend({"role": "user", "content": f"n{i}"} for i in range(15, 60))
        self.assertEqual(len(messages), 60)

        # tail_start would be 14 -- exactly the orphaned result. The repair
        # pulls it back to 13 so the pair travels together.
        result = compactor.snip_compact(messages)

        self.assertIsInstance(result[4].get("content"), list)
        self.assertEqual(result[4]["content"][0]["type"], "tool_use")
        self.assert_pairs_intact(result)

    def test_micro_compact_protects_unseen_and_recent(self):
        compactor = self.compactor()
        long_text = "L" * 500
        messages: list[dict] = [{"role": "user", "content": "start"}]
        for index in range(6):
            messages.append({"role": "assistant", "content": [tool_use(f"t{index}")]})
            messages.append({"role": "user", "content": [tool_result(f"t{index}", long_text)]})
        messages.append({"role": "assistant", "content": "thinking"})
        # one brand-new, unread result
        messages.append({"role": "user", "content": [tool_result("fresh", long_text)]})

        compactor.micro_compact(messages)

        bodies = [
            block["content"]
            for message in messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        pointer = "[Earlier tool result saved at "
        replaced = [body for body in bodies if body.startswith(pointer)]
        intact = [body for body in bodies if body == long_text]
        self.assertEqual(len(replaced), 3, "the three oldest consumed results")
        self.assertEqual(bodies[-1], long_text, "the unread result must survive")
        self.assertEqual(len(intact), 4, "newest three consumed + the fresh one")

    def test_fit_tool_results_touches_unseen_results(self):
        compactor = self.compactor()
        big = "B" * 5_000
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [tool_use("t")]},
            {"role": "user", "content": [tool_result("t", big)]},
        ]
        compactor.fit_tool_results(messages, target_chars=200)
        self.assertLess(len(messages[-1]["content"][0]["content"]), len(big))

    def test_compact_history_yields_one_summary_message(self):
        compactor = self.compactor(script=["The user asked for a parser."])
        messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]
        result = compactor.compact_history(messages, "add the parser")

        self.assertEqual(len(result), 1)
        self.assertIn("[Compacted]", result[0]["content"])
        self.assertIn("add the parser", result[0]["content"])
        self.assertIn("The user asked for a parser.", result[0]["content"])
        self.assertIn("Full transcript:", result[0]["content"])

    def test_reactive_compact_keeps_the_newest_messages(self):
        compactor = self.compactor(script=["Summary of the old part."])
        messages = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        result = compactor.reactive_compact(messages, "carry on")

        self.assertIn("[Reactive compact]", result[0]["content"])
        self.assertEqual(result[-1]["content"], "m9")
        self.assertEqual(len(result), 1 + compactor.KEEP_RECENT_MESSAGES)

    def test_summary_input_is_clipped(self):
        compactor = self.compactor()
        messages = [{"role": "user", "content": "q" * 200_000}]
        clipped = compactor.summary_input(messages)
        self.assertLessEqual(len(clipped), compactor.SUMMARY_INPUT_CHAR_LIMIT + 100)
        self.assertIn("middle omitted", clipped)

    def test_prepare_only_snips_below_the_limit(self):
        compactor = self.compactor()
        messages = [{"role": "user", "content": f"m{i}"} for i in range(60)]
        result = compactor.prepare(messages, "request")
        self.assertLess(len(result), 60)
        self.assertNotIn("[Compacted]", result[0]["content"])

    def test_prepare_summarizes_when_over_the_limit(self):
        compactor = self.compactor(script=["Heavy summary."])
        compactor.CONTEXT_CHAR_LIMIT = 500
        messages = [{"role": "user", "content": "q" * 2_000} for _ in range(6)]
        result = compactor.prepare(messages, "request")
        self.assertEqual(len(result), 1)
        self.assertIn("[Compacted]", result[0]["content"])

    def test_compaction_can_be_disabled(self):
        compactor = self.compactor(enabled=False)
        messages = [{"role": "user", "content": f"m{i}"} for i in range(60)]
        self.assertEqual(len(compactor.prepare(messages, "r")), 60)

    def test_the_compact_tool_replaces_history_after_the_batch(self):
        """The model can ask for room; the batch's side effects happen first."""
        runtime = self.make_runtime(
            script=[
                [
                    {"tool": "write_file", "input": {"path": "kept.txt", "content": "important"}},
                    {"tool": "compact", "input": {}},
                ],
                "Compacted and done.",
            ]
        )
        # Give the summarizer something to chew on.
        runtime.llm.script.insert(1, "Summary: wrote kept.txt.")
        answer = runtime.submit("write a file then compact")

        self.assertEqual(answer, "Compacted and done.")
        # The write landed on disk before the history was discarded.
        self.assertEqual((self.tmp / "kept.txt").read_text(encoding="utf-8"), "important")
        # History is now a single [Compacted] message.
        self.assertEqual(len(runtime.messages), 2)  # marker + final assistant turn
        self.assertIn("[Compacted]", runtime.messages[0]["content"])
        self.assertIn("write a file then compact", runtime.messages[0]["content"])
        self.assertIn("Summary: wrote kept.txt.", runtime.messages[0]["content"])

    def test_compact_tool_is_not_offered_when_compaction_is_disabled(self):
        runtime = self.make_runtime(compaction_enabled=False)
        self.assertNotIn("compact", runtime.tools)
        self.assertIn("compact", self.make_runtime().tools)

    def test_prompt_too_long_detection(self):
        self.assertTrue(is_prompt_too_long(RuntimeError("prompt_too_long: 250000 tokens")))
        self.assertTrue(is_prompt_too_long(RuntimeError("Too many tokens in request")))
        self.assertFalse(is_prompt_too_long(RuntimeError("connection reset")))

    def test_estimate_chars_matches_json_encoding(self):
        messages = [{"role": "user", "content": "héllo"}]
        self.assertEqual(
            ContextCompactor.estimate_chars(messages),
            len(json.dumps(messages, default=str, ensure_ascii=False)),
        )


# ==========================================================================
# Memory
# ==========================================================================


class MemoryPathTests(HarnessCase):
    def store(self, script=None) -> MemoryStore:
        return MemoryStore(
            MockLLM(script=script or []),
            "mock-1",
            self.tmp / ".agent" / "memory",
            self.tmp,
        )

    def test_slug_is_filesystem_safe(self):
        self.assertEqual(memory_slug("User's Preferred Editor!"), "user-s-preferred-editor")
        self.assertEqual(memory_slug("!!!"), "memory")

    def test_path_gates(self):
        store = self.store()
        with self.assertRaises(ValueError):
            store.memory_path("sub/record.md")
        with self.assertRaises(ValueError):
            store.memory_path("../escape.md")
        with self.assertRaises(ValueError):
            store.memory_path(INDEX_NAME)
        self.assertTrue(store.memory_path(INDEX_NAME, allow_index=True).name == INDEX_NAME)
        self.assertTrue(store.memory_path("ok.md").name == "ok.md")

    def test_write_creates_record_and_index(self):
        store = self.store()
        path = store.write_record("Preferred editor", "user", "The user prefers vim.", "Use vim.")
        self.assertTrue(path.is_file())
        index = store.read_index()
        self.assertIn("Preferred editor", index)
        self.assertIn("preferred-editor.md", index)
        self.assertIn("The user prefers vim.", index)
        self.assertEqual(len(store.list_records()), 1)

    def test_records_roundtrip_metadata(self):
        store = self.store()
        store.write_record("Build command", "project", "Builds with make.", "Run make.")
        record = store.list_records()[0]
        self.assertEqual(record["name"], "Build command")
        self.assertEqual(record["type"], "project")
        self.assertEqual(record["body"], "Run make.")

    def test_index_records_reads_the_catalog_without_loading_record_bodies(self):
        store = self.store(script=["not json"])
        store.write_record("Python version", "user", "The user uses Python 3.13.", "3.13")
        store.write_record("Deploy target", "project", "Deploys to staging.", "staging")

        self.assertEqual(
            store.index_records(),
            [
                {
                    "filename": "deploy-target.md",
                    "name": "Deploy target",
                    "description": "Deploys to staging.",
                },
                {
                    "filename": "python-version.md",
                    "name": "Python version",
                    "description": "The user uses Python 3.13.",
                },
            ],
        )

        def unexpected_full_read():
            raise AssertionError("selection must use MEMORY.md, not list_records()")

        store.list_records = unexpected_full_read  # type: ignore[method-assign]
        selected = store.select_relevant(
            [{"role": "user", "content": "which python should I use"}]
        )
        self.assertEqual(selected, ["python-version.md"])

    def test_index_records_ignores_malformed_stale_and_reserved_entries(self):
        store = self.store()
        store.write_record("Python version", "user", "The user uses Python 3.13.", "3.13")
        store.memory_path(INDEX_NAME, allow_index=True).write_text(
            "- malformed entry\n"
            "- [Missing](missing.md) - stale\n"
            "- [Index](MEMORY.md) - reserved\n"
            "- [Escape](../outside.md) - unsafe\n"
            "- [Python version](python-version.md) - The user uses Python 3.13.\n",
            encoding="utf-8",
        )

        self.assertEqual(
            store.index_records(),
            [
                {
                    "filename": "python-version.md",
                    "name": "Python version",
                    "description": "The user uses Python 3.13.",
                }
            ],
        )

    def test_missing_index_fails_closed_without_reading_records(self):
        store = self.store(script=["unused"])
        store.write_record("Python version", "user", "The user uses Python 3.13.", "3.13")
        store.memory_path(INDEX_NAME, allow_index=True).unlink()

        def unexpected_full_read():
            raise AssertionError("selection must not fall back to list_records()")

        store.list_records = unexpected_full_read  # type: ignore[method-assign]
        selected = store.select_relevant(
            [{"role": "user", "content": "which python should I use"}]
        )
        self.assertEqual(selected, [])
        self.assertEqual(len(store.llm.calls), 0)


class MemoryGateTests(HarnessCase):
    def test_validate_requires_all_four_fields(self):
        good = {"name": "n", "type": "user", "description": "d", "body": "b", "scope": "persistent"}
        self.assertIsNotNone(MemoryStore.validate_candidate(good, require_scope=True))
        for missing in ("name", "type", "description", "body"):
            broken = dict(good)
            broken[missing] = ""
            self.assertIsNone(MemoryStore.validate_candidate(broken), missing)

    def test_unknown_type_rejected(self):
        bad = {"name": "n", "type": "guess", "description": "d", "body": "b"}
        self.assertIsNone(MemoryStore.validate_candidate(bad))

    def test_scope_is_required_when_asked(self):
        no_scope = {"name": "n", "type": "user", "description": "d", "body": "b"}
        self.assertIsNone(MemoryStore.validate_candidate(no_scope, require_scope=True))
        self.assertIsNotNone(MemoryStore.validate_candidate(no_scope))

    def test_current_task_scope_is_never_stored(self):
        candidate = {
            "name": "Port",
            "type": "project",
            "description": "Port 8080 for now.",
            "body": "Use 8080.",
            "scope": "current_task",
        }
        self.assertFalse(MemoryStore.should_store(candidate, []))

    def test_temporary_markers_are_rejected(self):
        for phrase in ("for now", "this session", "当前任务", "このセッション"):
            candidate = {
                "name": "Thing",
                "type": "project",
                "description": f"Something {phrase}.",
                "body": "body",
                "scope": "persistent",
            }
            self.assertFalse(MemoryStore.should_store(candidate, []), phrase)

    def test_duplicates_are_rejected(self):
        candidate = {
            "name": "Preferred editor",
            "type": "user",
            "description": "Uses vim.",
            "body": "Always vim.",
            "scope": "persistent",
        }
        self.assertTrue(MemoryStore.should_store(candidate, []))
        existing = [{"name": "preferred editor", "description": "x", "body": "y"}]
        self.assertFalse(MemoryStore.should_store(candidate, existing))

    def test_a_durable_record_is_accepted(self):
        candidate = {
            "name": "Language",
            "type": "user",
            "description": "The user writes Python 3.13.",
            "body": "Prefer Python 3.13 features.",
            "scope": "persistent",
        }
        self.assertTrue(MemoryStore.should_store(candidate, []))


class MemoryExtractionTests(HarnessCase):
    def store(self, script) -> MemoryStore:
        return MemoryStore(
            MockLLM(script=script),
            "mock-1",
            self.tmp / ".agent" / "memory",
            self.tmp,
        )

    def test_extract_stores_persistent_records_only(self):
        payload = json.dumps(
            [
                {
                    "name": "Preferred editor",
                    "type": "user",
                    "scope": "persistent",
                    "description": "The user prefers vim.",
                    "body": "Use vim for edits.",
                },
                {
                    "name": "Scratch port",
                    "type": "project",
                    "scope": "current_task",
                    "description": "Use port 8080.",
                    "body": "Temporary.",
                },
            ]
        )
        store = self.store([payload])
        stored = store.extract([{"role": "user", "content": "I prefer vim."}])

        self.assertEqual(stored, 1)
        names = [record["name"] for record in store.list_records()]
        self.assertEqual(names, ["Preferred editor"])

    def test_extract_survives_a_broken_model(self):
        store = self.store(["not json at all"])
        self.assertEqual(store.extract([{"role": "user", "content": "hello"}]), 0)

    def test_extract_with_no_dialogue_makes_no_call(self):
        llm = MockLLM(script=["unused"])
        store = MemoryStore(llm, "mock-1", self.tmp / ".agent" / "memory", self.tmp)
        self.assertEqual(store.extract([]), 0)
        self.assertEqual(len(llm.calls), 0)

    def test_consolidation_merges_and_rewrites(self):
        store = self.store(
            [
                json.dumps(
                    [
                        {
                            "name": "Editor",
                            "type": "user",
                            "description": "Uses vim.",
                            "body": "vim",
                        }
                    ]
                )
            ]
        )
        for index in range(10):
            store.write_record(f"Record {index}", "project", f"Description {index}", f"Body {index}")
        self.assertEqual(len(store.list_records()), 10)

        self.assertEqual(store.consolidate(), 1)
        records = store.list_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "Editor")
        self.assertIn("Editor", store.read_index())

    def test_consolidation_is_skipped_below_the_threshold(self):
        store = self.store(["unused"])
        store.write_record("Only one", "user", "d", "b")
        self.assertEqual(store.consolidate(), 0)

    def test_consolidation_rolls_back_on_nonsense(self):
        store = self.store(["[]"])
        for index in range(10):
            store.write_record(f"Record {index}", "project", f"Description {index}", f"Body {index}")
        before = sorted(record["name"] for record in store.list_records())

        self.assertEqual(store.consolidate(), 0)
        self.assertEqual(sorted(record["name"] for record in store.list_records()), before)

    def test_selection_falls_back_to_keywords(self):
        store = self.store(["not json"])
        store.write_record("Python version", "user", "The user uses Python 3.13.", "3.13")
        store.write_record("Deploy target", "project", "Deploys to staging.", "staging")

        selected = store.select_relevant([{"role": "user", "content": "which python should I use"}])
        self.assertEqual(selected, ["python-version.md"])

    def test_relevant_records_are_injected_within_budget(self):
        store = self.store(["not json"])
        store.write_record("Python version", "user", "The user uses Python 3.13.", "3.13")
        payload = store.load_relevant([{"role": "user", "content": "python version?"}])
        self.assertIn("python-version.md", payload)
        self.assertLessEqual(len(payload), RECALL_CHAR_LIMIT + 500)

    def test_load_relevant_reads_only_the_selected_record_body(self):
        store = self.store(["[1]"])
        store.write_record("Python version", "user", "The user uses Python 3.13.", "3.13")
        store.write_record("Deploy target", "project", "Deploys to staging.", "staging")

        original_read_record = store.read_record
        reads: list[str] = []

        def tracked_read_record(filename: str) -> str | None:
            reads.append(filename)
            return original_read_record(filename)

        store.read_record = tracked_read_record  # type: ignore[method-assign]
        payload = store.load_relevant([{"role": "user", "content": "python version?"}])

        self.assertEqual(reads, ["python-version.md"])
        self.assertIn("python-version.md", payload)
        self.assertIn("3.13", payload)
        self.assertNotIn("staging", payload)

    def test_index_and_selection_appear_in_the_system_prompt(self):
        runtime = self.make_runtime(memory=True)
        runtime.memory.write_record("Python version", "user", "The user uses Python 3.13.", "3.13")
        prompt = runtime.build_system()
        self.assertIn("Memory catalog:", prompt)
        self.assertIn("Python version", prompt)

    def test_extraction_runs_on_the_stop_hook(self):
        runtime = self.make_runtime(
            script=[
                "I'll remember that.",
                json.dumps(
                    [
                        {
                            "name": "Preferred editor",
                            "type": "user",
                            "scope": "persistent",
                            "description": "The user prefers vim.",
                            "body": "Use vim.",
                        }
                    ]
                ),
            ],
            memory=True,
        )
        runtime.submit("I always use vim, remember that")
        names = [record["name"] for record in runtime.memory.list_records()]
        self.assertIn("Preferred editor", names)

    def test_extract_json_array_tolerates_prose_and_fences(self):
        self.assertEqual(extract_json_array('Sure:\n```json\n[1, 2]\n```'), [1, 2])
        self.assertEqual(extract_json_array("no array here"), [])
        self.assertEqual(extract_json_array('prefix [{"a": 1}] suffix'), [{"a": 1}])

    def test_memory_selection_is_cached_within_a_turn(self):
        """Selection costs a model call; it must not run per loop iteration.

        `build_system` is invoked before every model call, but selection is a
        per-turn decision, so caching it is the difference between one extra
        call per turn and one per tool round.
        """
        runtime = self.make_runtime(memory=True)
        runtime.memory.write_record("Python version", "user", "The user uses Python 3.13.", "3.13")
        runtime.messages = [{"role": "user", "content": "which python should I use?"}]

        before = len(runtime.llm.calls)
        runtime.build_system()
        runtime.build_system()
        runtime.build_system()
        self.assertEqual(
            len(runtime.llm.calls) - before, 1, "selection must run once per turn"
        )

        # A new turn re-selects, because the request changed.
        runtime.messages.append({"role": "assistant", "content": "done"})
        runtime.submit("now something else entirely")
        self.assertGreater(len(runtime.llm.calls), before + 1)

    def test_one_tool_round_costs_one_worker_call_plus_extraction(self):
        """A turn with N tool rounds must make N+1 worker calls, no more.

        The mock is shared by the worker and by the memory bookkeeping calls,
        so the responder routes on the tool list: bookkeeping calls carry no
        tools and must not consume the worker's script.
        """
        worker_turns = [
            {"tool": "bash", "input": {"command": "echo a"}},
            {"tool": "bash", "input": {"command": "echo b"}},
            "done",
        ]

        def responder(_index, _messages, tools):
            if tools:
                return worker_turns.pop(0) if worker_turns else "done"
            return "[]"  # selection finds nothing; extraction stores nothing

        runtime = self.make_runtime(responder=responder, memory=True)
        runtime.memory.write_record("Python version", "user", "The user uses Python 3.13.", "3.13")
        runtime.submit("run two commands")

        with_tools = [call for call in runtime.llm.calls if call["tools"]]
        without = [call for call in runtime.llm.calls if not call["tools"]]

        self.assertEqual(len(with_tools), 3, "2 tool rounds + 1 final answer")
        self.assertEqual(
            len(without), 2,
            "exactly one selection and one extraction for the whole turn",
        )


# ==========================================================================
# Teams
# ==========================================================================


class MessageBusTests(HarnessCase):
    def bus(self) -> MessageBus:
        return MessageBus(self.tmp / ".agent" / "mailbox")

    def test_send_and_consume(self):
        bus = self.bus()
        bus.send("lead", "worker", "hello", metadata={"k": "v"})
        messages = bus.read_inbox("worker")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].sender, "lead")
        self.assertEqual(messages[0].content, "hello")
        self.assertEqual(messages[0].metadata["k"], "v")
        # destructive read
        self.assertEqual(bus.read_inbox("worker"), [])

    def test_peek_does_not_consume(self):
        bus = self.bus()
        bus.send("lead", "worker", "hi")
        self.assertEqual(len(bus.peek("worker")), 1)
        self.assertEqual(len(bus.read_inbox("worker")), 1)

    def test_ordering_is_preserved(self):
        bus = self.bus()
        for index in range(5):
            bus.send("lead", "worker", f"msg{index}")
        self.assertEqual([m.content for m in bus.read_inbox("worker")], [f"msg{i}" for i in range(5)])

    def test_invalid_agent_names_rejected(self):
        bus = self.bus()
        for bad in ("../escape", "with space", "", "x" * 100):
            with self.assertRaises(Exception):
                bus.send("lead", bad, "x")
        self.assertFalse(is_valid_agent_name("../escape"))
        self.assertTrue(is_valid_agent_name("worker-1"))

    def test_wait_for_messages_wakes_on_send(self):
        bus = self.bus()
        received: list[list] = []

        def waiter() -> None:
            received.append(bus.wait_for_messages("worker", timeout=5.0))

        thread = threading.Thread(target=waiter)
        started = time.monotonic()
        thread.start()
        time.sleep(0.1)
        bus.send("lead", "worker", "wake up")
        thread.join(timeout=5.0)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0].content, "wake up")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_wait_times_out_cleanly(self):
        bus = self.bus()
        start = time.monotonic()
        self.assertEqual(bus.wait_for_messages("worker", timeout=0.3), [])
        self.assertLess(time.monotonic() - start, 2.0)


class ProtocolTests(HarnessCase):
    def test_request_id_format_and_uniqueness(self):
        state = ProtocolState()
        first = state.create("plan", "worker")
        second = state.create("plan", "worker")
        self.assertRegex(first.request_id, r"^req_\d{6}$")
        self.assertNotEqual(first.request_id, second.request_id)

    def test_match_response_validates_kind_sender_and_liveness(self):
        state = ProtocolState()
        request = state.create("plan", "worker")

        self.assertIsNone(state.match_response(PLAN_RESPONSE, request.request_id, "impostor"))
        self.assertIsNone(state.match_response("shutdown_response", request.request_id, "worker"))
        self.assertIsNone(state.match_response(PLAN_RESPONSE, "req_999999", "worker"))
        self.assertIsNotNone(state.match_response(PLAN_RESPONSE, request.request_id, "worker"))

        state.resolve(request, True)
        self.assertIsNone(
            state.match_response(PLAN_RESPONSE, request.request_id, "worker"),
            "a resolved request must not be matched again",
        )

    def test_plan_gate_follows_the_decision(self):
        state = ProtocolState()
        request = state.create("plan", "worker")
        self.assertFalse(state.plan_approved("worker"))
        state.resolve(request, True)
        self.assertTrue(state.plan_approved("worker"))

    def test_approval_is_invalidated_when_work_changes(self):
        state = ProtocolState()
        request = state.create("plan", "worker")
        state.resolve(request, True)
        self.assertTrue(state.plan_approved("worker"))

        version = state.bump_work_version("worker")
        self.assertFalse(state.plan_approved("worker", version - 1))
        self.assertTrue(state.plan_approved("worker", version))

    def test_not_required_is_always_approved(self):
        state = ProtocolState()
        state.plan_gates["worker"] = "not_required"
        self.assertTrue(state.plan_approved("worker"))


class WorktreeTests(HarnessCase):
    def test_name_validation(self):
        self.assertEqual(validate_worktree_name("parser-work"), "parser-work")
        for bad in ("-leading", "a" * 70, "has space", "dot..dot", ""):
            with self.assertRaises(Exception):
                validate_worktree_name(bad)

    def test_non_git_workspace_reports_clearly(self):
        # Stop Git from discovering an outer repository when this test suite
        # itself is run from a Git checkout.
        (self.tmp / ".git").write_text("gitdir: nonexistent\n", encoding="utf-8")
        manager = WorktreeManager(self.tmp, self.tmp / ".agent" / "worktrees")
        self.assertFalse(manager.is_git_repo())
        result = manager.create("wt", "task_00000000")
        self.assertIn("not a git repository", result)

    def test_cwd_falls_back_to_the_workspace_without_an_assignment(self):
        manager = WorktreeManager(self.tmp, self.tmp / ".agent" / "worktrees")
        self.assertEqual(manager.cwd_for("worker"), self.tmp)

    def test_disabled_worktrees_refuse(self):
        manager = WorktreeManager(self.tmp, self.tmp / ".agent" / "worktrees", enabled=False)
        self.assertIn("disabled", manager.create("wt", "task_00000000"))

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_create_assign_lease_and_remove(self):
        import subprocess

        repo = self.tmp
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        (repo / "README.md").write_text("seed\n", encoding="utf-8")
        for command in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "init"],
        ):
            subprocess.run(command, cwd=repo, check=True, capture_output=True, env=env)

        manager = WorktreeManager(repo, repo / ".agent" / "worktrees")
        self.assertTrue(manager.is_git_repo())

        outcome = manager.create("parser-work", "task_00000000")
        self.assertTrue(outcome.startswith("Created"), outcome)
        worktree_path = repo / ".agent" / "worktrees" / "parser-work"
        self.assertTrue(worktree_path.is_dir())

        # Refuses to remove while leased.
        manager.assign("worker", "task_00000000", "parser-work")
        self.assertIn("still assigned", manager.remove("parser-work"))
        self.assertEqual(manager.cwd_for("worker"), worktree_path)

        manager.release("worker")
        self.assertEqual(manager.cwd_for("worker"), repo)
        self.assertTrue(manager.remove("parser-work").startswith("Removed"))


class TeammateTests(HarnessCase):
    """Drive one teammate synchronously, so the assertions are deterministic."""

    def build(
        self,
        script,
        *,
        autonomous: bool = False,
        require_plan: bool = False,
        idle_rounds: int = 0,
    ) -> tuple[Teammate, MockLLM, TaskStore, MessageBus, ProtocolState]:
        settings = make_settings(self.tmp)
        llm = MockLLM(script=script)
        tasks = TaskStore(settings.tasks_dir)
        bus = MessageBus(settings.mailbox_dir)
        protocol = ProtocolState()
        worktrees = WorktreeManager(settings.workdir, settings.worktrees_dir)
        runtime = self.make_runtime()

        # The same pool a real teammate gets, minus the pieces that need a
        # live team: `submit_plan` is exercised directly on the teammate.
        registry = ToolRegistry()
        register_basic_tools(registry)
        register_task_tools(registry)
        register_teammate_team_tools(registry)

        tool_runtime = TeammateToolRuntime(
            teams=None,  # type: ignore[arg-type]
            tasks=tasks,
            teammate=None,  # type: ignore[arg-type]
            settings=settings,
        )

        teammate = Teammate(
            name="worker",
            role="writer",
            prompt="Do the work.",
            bus=bus,
            tasks=tasks,
            worktrees=worktrees,
            protocol=protocol,
            registry=registry,
            llm=llm,
            settings=settings,
            hooks_factory=Hooks,
            runner=runtime.loop_runner,
            tool_runtime=tool_runtime,
            autonomous=autonomous,
            require_plan=require_plan,
            idle_rounds=idle_rounds,
        )
        return teammate, llm, tasks, bus, protocol

    def test_teammate_runs_a_turn_and_reports(self):
        teammate, llm, _tasks, _bus, _protocol = self.build(
            script=[{"tool": "bash", "input": {"command": "echo teammate-ran"}}, "I finished the work."]
        )
        teammate.run()

        self.assertEqual(teammate.state.status, "stopped")
        self.assertGreaterEqual(teammate.state.turns, 1)
        self.assertIn("finished", teammate.state.last_output)
        self.assertEqual(len(llm.calls), 2)

    def test_teammate_context_uses_its_own_todos(self):
        teammate, _llm, _tasks, _bus, _protocol = self.build(script=[])
        context = teammate._tool_context()
        self.assertIs(context.runtime.todos, teammate.todos)
        run_todo_write({"todos": [{"content": "private step"}]}, context)
        self.assertEqual(teammate.todos.items[0].content, "private step")

    def test_newly_claimed_task_clears_previous_todos(self):
        teammate, _llm, tasks, _bus, _protocol = self.build(script=[], autonomous=True)
        teammate.todos.replace([{"content": "previous task step"}])
        task = tasks.create("New task")

        self.assertEqual(teammate._find_work(), (True, True))
        self.assertEqual(teammate.state.claimed_task, task.id)
        self.assertEqual(teammate.todos.total, 0)

    def test_message_arriving_during_idle_wait_runs_a_turn(self):
        teammate, _llm, _tasks, bus, _protocol = self.build(
            script=[], autonomous=False, idle_rounds=2
        )
        original_wait = bus.wait_for_messages
        sent = False
        seen: list[str] = []

        def wait_with_message(agent: str, timeout: float = 0) -> list:
            nonlocal sent
            if not sent:
                sent = True
                bus.send("lead", agent, "Please review this note.")
            return original_wait(agent, timeout=0)

        def run_turn() -> bool:
            seen.append(str(teammate.messages[-1]["content"]))
            teammate.stop()
            return False

        bus.wait_for_messages = wait_with_message
        teammate._run_turn = run_turn
        teammate._work_loop(0)

        self.assertEqual(len(seen), 1)
        self.assertIn("Please review this note.", seen[0])

    def test_shutdown_in_inbox_stops_without_another_model_turn(self):
        teammate, _llm, _tasks, bus, _protocol = self.build(
            script=[], autonomous=False, idle_rounds=1
        )
        bus.send("lead", "worker", "Stop now.", type=SHUTDOWN_REQUEST,
                 metadata={"request_id": "req_000001"})
        calls = 0

        def run_turn() -> bool:
            nonlocal calls
            calls += 1
            return False

        teammate._run_turn = run_turn
        teammate._work_loop(0)

        self.assertEqual(calls, 0)
        self.assertTrue(teammate._stop.is_set())
        self.assertEqual(bus.peek("lead")[0].type, SHUTDOWN_RESPONSE)

    def test_shutdown_arriving_during_wait_does_not_run_model(self):
        teammate, _llm, _tasks, bus, _protocol = self.build(
            script=[], autonomous=False, idle_rounds=1
        )
        original_wait = bus.wait_for_messages
        sent = False
        calls = 0

        def wait_with_shutdown(agent: str, timeout: float = 0) -> list:
            nonlocal sent
            if not sent:
                sent = True
                bus.send("lead", agent, "Stop now.", type=SHUTDOWN_REQUEST,
                         metadata={"request_id": "req_000001"})
            return original_wait(agent, timeout=0)

        def run_turn() -> bool:
            nonlocal calls
            calls += 1
            return False

        bus.wait_for_messages = wait_with_shutdown
        teammate._run_turn = run_turn
        teammate._work_loop(0)

        self.assertEqual(calls, 0)
        self.assertTrue(teammate._stop.is_set())
        self.assertEqual(bus.peek("lead")[0].type, SHUTDOWN_RESPONSE)

    def test_plan_decision_arriving_during_wait_reaches_next_turn(self):
        teammate, _llm, _tasks, bus, protocol = self.build(
            script=[], autonomous=False, require_plan=True, idle_rounds=2
        )
        protocol.plan_gates["worker"] = "pending"
        original_wait = bus.wait_for_messages
        sent = False
        seen: list[str] = []

        def wait_with_decision(agent: str, timeout: float = 0) -> list:
            nonlocal sent
            if not sent:
                sent = True
                bus.send("lead", agent, "Approved.", type=PLAN_DECISION,
                         metadata={"approve": True, "feedback": "Proceed"})
            return original_wait(agent, timeout=0)

        def run_turn() -> bool:
            seen.append(str(teammate.messages[-1]["content"]))
            teammate.stop()
            return False

        bus.wait_for_messages = wait_with_decision
        teammate._run_turn = run_turn
        teammate._work_loop(0)

        self.assertEqual(protocol.plan_gates["worker"], "approved")
        self.assertEqual(len(seen), 1)
        self.assertIn("approved", seen[0])
        self.assertNotIn("awaiting the lead's decision", seen[0])

    def test_no_task_or_message_waits_once_then_retires(self):
        teammate, _llm, _tasks, bus, _protocol = self.build(
            script=[], autonomous=False, idle_rounds=1
        )
        waits = 0
        turns = 0

        def empty_wait(agent: str, timeout: float = 0) -> list:
            nonlocal waits
            waits += 1
            return []

        def run_turn() -> bool:
            nonlocal turns
            turns += 1
            return False

        bus.wait_for_messages = empty_wait
        teammate._run_turn = run_turn
        teammate._work_loop(0)

        self.assertEqual(waits, 1)
        self.assertEqual(turns, 0)
        self.assertIn("retiring", teammate.state.last_message)

    def test_task_created_during_wait_is_claimed_next_round(self):
        teammate, _llm, tasks, bus, _protocol = self.build(
            script=[], autonomous=True, idle_rounds=2
        )
        task_id = ""
        waits = 0
        seen: list[str | None] = []

        def create_task_on_wait(agent: str, timeout: float = 0) -> list:
            nonlocal task_id, waits
            waits += 1
            if waits == 1:
                task_id = tasks.create("New work").id
            return []

        def run_turn() -> bool:
            seen.append(teammate.state.claimed_task)
            teammate.stop()
            return False

        bus.wait_for_messages = create_task_on_wait
        teammate._run_turn = run_turn
        teammate._work_loop(0)

        self.assertEqual(seen, [task_id])

    def test_open_task_without_new_input_does_not_recall_model(self):
        teammate, _llm, tasks, bus, _protocol = self.build(
            script=[], autonomous=True, idle_rounds=2
        )
        task = tasks.create("Waiting on lead")
        turns = 0
        waits = 0

        def run_turn() -> bool:
            nonlocal turns
            turns += 1
            return False

        def empty_wait(agent: str, timeout: float = 0) -> list:
            nonlocal waits
            waits += 1
            return []

        teammate._run_turn = run_turn
        bus.wait_for_messages = empty_wait
        teammate._work_loop(0)

        self.assertEqual(teammate.state.claimed_task, task.id)
        self.assertEqual(turns, 1)
        self.assertEqual(waits, 2)

    def test_pending_plan_waits_for_decision_before_next_turn(self):
        teammate, _llm, tasks, bus, protocol = self.build(
            script=[], autonomous=True, require_plan=True, idle_rounds=1
        )
        tasks.create("Plan-gated work")
        original_wait = bus.wait_for_messages
        turns = 0
        waits = 0
        gates: list[str | None] = []

        def run_turn() -> bool:
            nonlocal turns
            turns += 1
            gates.append(protocol.plan_gates.get("worker"))
            if turns == 1:
                protocol.plan_gates["worker"] = "pending"
                return True  # submit_plan was a tool call, but now needs approval.
            teammate.stop()
            return False

        def wait_for_decision(agent: str, timeout: float = 0) -> list:
            nonlocal waits
            waits += 1
            if waits == 3:
                bus.send("lead", agent, "Approved.", type=PLAN_DECISION,
                         metadata={"approve": True})
            return original_wait(agent, timeout=0)

        teammate._run_turn = run_turn
        bus.wait_for_messages = wait_for_decision
        teammate._work_loop(0)

        self.assertEqual(turns, 2)
        self.assertEqual(waits, 3)
        self.assertEqual(gates[-1], "approved")

    def test_task_replaced_during_wait_runs_new_task(self):
        teammate, _llm, tasks, bus, _protocol = self.build(
            script=[], autonomous=True, idle_rounds=3
        )
        first = tasks.create("First task")
        next_task_id = ""
        seen: list[str | None] = []
        waits = 0

        def run_turn() -> bool:
            seen.append(teammate.state.claimed_task)
            if len(seen) == 2:
                teammate.stop()
            return False

        def replace_during_wait(agent: str, timeout: float = 0) -> list:
            nonlocal waits, next_task_id
            waits += 1
            if waits == 1:
                completed = tasks.load(first.id)
                completed.status = COMPLETED
                tasks.save(completed)
                next_task_id = tasks.create("Next task").id
            return []

        teammate._run_turn = run_turn
        bus.wait_for_messages = replace_during_wait
        teammate._work_loop(0)

        self.assertEqual(seen, [first.id, next_task_id])

    def test_every_exit_path_returns_an_unfinished_task_to_the_board(self):
        """A teammate that stops mid-task must not orphan its work."""
        teammate, _llm, tasks, _bus, _protocol = self.build(
            script=["I'll start on it."], autonomous=True
        )
        task = tasks.create("Half-finished work")
        teammate.run()

        # The open loop above claims the task, does one turn, then retires with
        # it still in_progress -- which is exactly the leak being guarded.
        stored = tasks.load(task.id)
        self.assertEqual(
            stored.status, PENDING,
            "a stopped teammate must hand its unfinished task back",
        )
        self.assertIsNone(stored.owner)
        self.assertIn(task.id, [t.id for t in tasks.ready()])

    def test_teammate_claims_and_completes_a_task(self):
        teammate, _llm, tasks, _bus, _protocol = self.build(
            script=[
                "I'll check the board.",          # briefing turn
                {"tool": "complete_task", "input": {"task_id": "PLACEHOLDER"}},
                "Task done.",
            ],
            autonomous=True,
        )
        task = tasks.create("Write the parser")
        teammate.llm.script[1] = {
            "tool": "complete_task",
            "input": {"task_id": task.id},
        }
        teammate.run()

        self.assertEqual(tasks.load(task.id).status, COMPLETED)
        self.assertEqual(tasks.load(task.id).owner, "worker")
        self.assertGreaterEqual(teammate.state.turns, 2, "briefing + work turn")

    def test_teammate_system_prompt_states_it_cannot_ask_a_human(self):
        teammate, _llm, _tasks, _bus, _protocol = self.build(script=["x"])
        prompt = teammate.system_prompt()
        self.assertIn("cannot ask a human", prompt)
        self.assertIn("worker", prompt)

    def test_plan_gate_blocks_until_approved(self):
        teammate, _llm, _tasks, bus, protocol = self.build(
            script=["Waiting for approval."], require_plan=True
        )
        protocol.plan_gates["worker"] = "pending"
        self.assertFalse(teammate._plan_approved())

        bus.send(
            "lead",
            "worker",
            "Plan approved.",
            type=PLAN_DECISION,
            metadata={"request_id": "req_000001", "approve": True, "feedback": "go ahead"},
        )
        summaries = teammate.handle_inbox()
        self.assertTrue(any("approved" in summary for summary in summaries))
        self.assertTrue(teammate._plan_approved())

    def test_plan_gate_actually_blocks_workspace_writes(self):
        """The gate must have teeth: a pending plan blocks bash and writes."""
        runtime = self.make_runtime(require_plan=True)
        outcome = runtime.teams.spawn(
            "worker", "implementer", "Write the parser.", autonomous=False
        )
        self.assertIn("Spawned", outcome)
        teammate = runtime.teams.get("worker")
        teammate.idle_rounds = 0

        ctx = teammate._tool_context()
        hooks = runtime._teammate_hooks()

        write_block = {"type": "tool_use", "id": "1", "name": "write_file",
                       "input": {"path": "parser.py", "content": "x"}}
        bash_block = {"type": "tool_use", "id": "2", "name": "bash",
                      "input": {"command": "echo hi"}}
        read_block = {"type": "tool_use", "id": "3", "name": "read_file",
                      "input": {"path": "parser.py"}}

        # Gate armed as "required" at spawn: writes and shell are blocked.
        self.assertEqual(runtime.teams.protocol.plan_gates["worker"], "required")
        for block in (write_block, bash_block):
            decision = hooks.trigger(PRE_TOOL_USE, block, ctx)
            self.assertIsNotNone(decision, block["name"])
            self.assertIn("Blocked: plan status is required", decision)

        # Reading stays open so a plan can be researched.
        self.assertIsNone(hooks.trigger(PRE_TOOL_USE, read_block, ctx))
        self.assertFalse((self.tmp / "parser.py").exists(), "nothing may be written")

        # Approving the plan opens exactly the gated tools.
        runtime.teams.protocol.plan_gates["worker"] = "approved"
        self.assertIsNone(hooks.trigger(PRE_TOOL_USE, write_block, ctx))

        runtime.close()

    def test_plan_gate_is_open_when_plan_is_not_required(self):
        runtime = self.make_runtime(require_plan=False)
        runtime.teams.spawn("writer", "implementer", "Do it.", autonomous=False)
        teammate = runtime.teams.get("writer")
        teammate.idle_rounds = 0

        self.assertEqual(runtime.teams.protocol.plan_gates["writer"], "not_required")
        ctx = teammate._tool_context()
        hooks = runtime._teammate_hooks()
        block = {"type": "tool_use", "id": "1", "name": "write_file",
                 "input": {"path": "free.py", "content": "x"}}
        self.assertIsNone(hooks.trigger(PRE_TOOL_USE, block, ctx))
        runtime.close()

    def test_the_lead_is_never_plan_gated(self):
        runtime = self.make_runtime(require_plan=True)
        block = {"type": "tool_use", "id": "1", "name": "write_file",
                 "input": {"path": "lead.py", "content": "x"}}
        hooks = runtime._teammate_hooks()
        self.assertIsNone(hooks.trigger(PRE_TOOL_USE, block, runtime.lead_ctx))

    def test_shutdown_request_is_acknowledged_and_stops_the_teammate(self):
        teammate, _llm, _tasks, bus, _protocol = self.build(script=["x"])
        bus.send(
            "lead",
            "worker",
            "stop",
            type=SHUTDOWN_REQUEST,
            metadata={"request_id": "req_000009"},
        )
        summaries = teammate.handle_inbox()
        self.assertTrue(any("shutdown" in summary for summary in summaries))
        self.assertTrue(teammate._stop.is_set())

        ack = bus.read_inbox("lead")
        self.assertEqual(len(ack), 1)
        self.assertEqual(ack[0].type, SHUTDOWN_RESPONSE)
        self.assertEqual(ack[0].metadata["request_id"], "req_000009")

    def test_submit_plan_without_a_lead_request_creates_one(self):
        teammate, _llm, tasks, bus, protocol = self.build(script=[], require_plan=True)
        teammate.worktrees.enabled = False
        task = tasks.create("Write parser")
        teammate.claim_task(task.id)

        outcome = teammate.submit_plan("Read the code, implement the parser, then test.")

        request_id = protocol.plan_request_ids["worker"]
        request = protocol.pending[request_id]
        self.assertIn(request_id, outcome)
        self.assertEqual(request.kind, "plan")
        self.assertEqual(request.teammate, "worker")
        self.assertEqual(request.task_id, task.id)
        self.assertEqual(request.work_version, protocol.work_versions["worker"])
        self.assertFalse(request.resolved)
        self.assertEqual(protocol.plan_gates["worker"], "pending")
        response = bus.read_inbox("lead")[0]
        self.assertEqual(response.type, PLAN_RESPONSE)
        self.assertEqual(response.metadata["request_id"], request_id)
        self.assertIs(protocol.match_response(response.type, request_id, response.sender), request)

    def test_submit_plan_reuses_an_outstanding_lead_request(self):
        teammate, _llm, _tasks, bus, protocol = self.build(script=[], require_plan=True)
        request = protocol.create("plan", "worker")
        teammate.submit_plan("First version")
        teammate.submit_plan("Updated version")

        responses = bus.read_inbox("lead")
        self.assertEqual(len(protocol.pending), 1)
        self.assertEqual([m.metadata["request_id"] for m in responses], [request.request_id] * 2)
        self.assertEqual(responses[-1].content, "Updated version")

    def test_submit_plan_after_a_decision_creates_a_new_review(self):
        for approved in (False, True):
            with self.subTest(approved=approved):
                teammate, _llm, _tasks, bus, protocol = self.build(script=[], require_plan=True)
                teammate.submit_plan("Original plan")
                previous = protocol.pending[protocol.plan_request_ids["worker"]]
                protocol.resolve(previous, approved, "Decision")
                bus.read_inbox("lead")

                teammate.submit_plan("Revised plan")

                current_id = protocol.plan_request_ids["worker"]
                self.assertNotEqual(current_id, previous.request_id)
                self.assertTrue(previous.resolved)
                self.assertFalse(protocol.pending[current_id].resolved)
                self.assertEqual(protocol.plan_gates["worker"], "pending")
                self.assertFalse(protocol.plan_approved("worker"))
                self.assertEqual(bus.read_inbox("lead")[0].metadata["request_id"], current_id)


class TeamManagerTests(HarnessCase):
    def manager(self, runtime=None):
        runtime = runtime or self.make_runtime()
        return runtime, runtime.teams

    def test_roster_and_name_validation(self):
        _runtime, teams = self.manager()
        self.assertEqual(teams.roster(), "No teammates.")
        self.assertIn("invalid teammate name", teams.spawn("../bad", "role", "prompt"))
        # Runtime identities are reserved, case-insensitively.
        for reserved in ("lead", "agent", "Lead", "AGENT"):
            self.assertIn("reserved by the runtime", teams.spawn(reserved, "role", "prompt"))
        self.assertFalse(teams.teammates)

    def test_plan_request_and_review_round_trip(self):
        _runtime, teams = self.manager()
        outcome = teams.request_plan("worker", "add the parser")
        self.assertIn("req_", outcome)
        request_id = outcome.split("request ", 1)[1].rstrip(")")

        # the teammate replies
        teams.bus.send(
            "worker",
            "lead",
            "1. read\n2. write",
            type=PLAN_RESPONSE,
            metadata={"request_id": request_id, "approve": True, "plan": "1. read\n2. write"},
        )
        events = teams.consume_lead_inbox()
        self.assertTrue(any("submitted a plan" in event for event in events))
        self.assertIn("review_plan", events[0])

        decision = teams.review_plan(request_id, True, "looks good")
        self.assertIn("approved", decision)
        self.assertEqual(teams.protocol.plan_gates["worker"], "approved")
        inbox = teams.bus.read_inbox("worker")
        self.assertEqual([message.type for message in inbox], [PLAN_REQUEST, PLAN_DECISION])
        self.assertEqual(inbox[1].metadata["request_id"], request_id)

    def test_stale_plan_response_is_ignored(self):
        _runtime, teams = self.manager()
        teams.request_plan("worker", "task")
        teams.bus.send(
            "worker",
            "lead",
            "late reply",
            type=PLAN_RESPONSE,
            metadata={"request_id": "req_999999", "approve": True},
        )
        events = teams.consume_lead_inbox()
        self.assertTrue(any("stale" in event for event in events))

    def test_double_review_is_rejected(self):
        _runtime, teams = self.manager()
        request_id = teams.request_plan("worker", "task").split("request ", 1)[1].rstrip(")")
        teams.review_plan(request_id, True)
        self.assertIn("already resolved", teams.review_plan(request_id, False))

    def test_pending_requests_listing(self):
        _runtime, teams = self.manager()
        teams.request_plan("worker", "task")
        self.assertIn("plan", teams.pending_requests())

    def test_shutdown_round_trip(self):
        _runtime, teams = self.manager()
        outcome = teams.shutdown("ghost")
        self.assertIn("no teammate", outcome)

    def test_teammate_worktree_lease_routes_tool_calls(self):
        settings = make_settings(self.tmp)
        manager = WorktreeManager(settings.workdir, settings.worktrees_dir)
        # no assignment -> the workspace itself
        self.assertEqual(manager.cwd_for("worker"), settings.workdir)
        assignment = manager.assign("worker", "task_00000000", "parser-work")
        self.assertEqual(assignment, settings.worktrees_dir / "parser-work")
        # the directory does not exist, so the lease falls back safely
        self.assertEqual(manager.cwd_for("worker"), settings.workdir)

    def test_teammate_registry_excludes_spawning(self):
        runtime = self.make_runtime()
        registry = runtime._teammate_registry()
        self.assertIn("bash", registry)
        self.assertIn("claim_task", registry)
        self.assertIn("submit_plan", registry)
        self.assertNotIn("spawn_teammate", registry)
        self.assertNotIn("task", registry)

    def test_tool_runtime_denies_subagent_spawning(self):
        runtime = self.make_runtime()
        adapter = runtime.teams.tool_runtime
        self.assertIn("cannot spawn subagents", adapter.spawn_subagent("x"))

    def test_teammate_adapter_is_per_teammate(self):
        runtime = self.make_runtime()
        adapter = runtime.teams.tool_runtime

        class FakeTeammate:
            name = "worker"
            todos = TodoList()

        specific = adapter.for_teammate(FakeTeammate())
        self.assertIs(specific.teammate.name, "worker")
        self.assertIsNot(specific, adapter)
        self.assertIs(specific.todos, FakeTeammate.todos)

    def test_teammate_todo_writes_are_isolated(self):
        runtime = self.make_runtime()
        adapter = runtime.teams.tool_runtime

        class FakeTeammate:
            def __init__(self, name):
                self.name = name
                self.todos = TodoList()

        first = FakeTeammate("first")
        second = FakeTeammate("second")

        def context(teammate):
            return ToolContext(
                settings=runtime.settings,
                workdir=runtime.settings.workdir,
                owner=teammate.name,
                interactive=False,
                runtime=adapter.for_teammate(teammate),
            )

        run_todo_write({"todos": [{"content": "first step"}]}, context(first))
        run_todo_write({"todos": [{"content": "second step"}]}, context(second))

        self.assertEqual(first.todos.items[0].content, "first step")
        self.assertEqual(second.todos.items[0].content, "second step")
        self.assertEqual(runtime.todos.total, 0)


class TeamIntegrationTests(HarnessCase):
    """End to end: the lead delegates, a real teammate thread does the work.

    One `responder` drives both agents.  It tells them apart by their tool
    pools -- the lead's pool contains `spawn_teammate`, a teammate's does not --
    which is also the property that bounds recursion.
    """

    @staticmethod
    def _all_text(messages: list[dict]) -> str:
        parts: list[str] = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(str(block.get("text", "")))
        return "\n".join(parts)

    def test_teammate_submits_and_waits_for_review_without_request_plan(self):
        runtime = self.make_runtime(require_plan=True)
        runtime.teams.worktrees.enabled = False
        task = runtime.tasks.create("Write parser.py", "Create the parser and verify it.")
        runtime.llm.script = [
            {"tool": "submit_plan", "input": {"plan": "Create parser.py, then verify it."}},
            {"tool": "write_file", "input": {"path": "parser.py", "content": "value = 1\n"}},
            "Waiting for approval.",
            {"tool": "write_file", "input": {"path": "parser.py", "content": "value = 1\n"}},
            {"tool": "complete_task", "input": {"task_id": task.id}},
            "Finished.",
        ]
        # Drive the real loops synchronously to inspect the approval boundary.
        from unittest.mock import patch
        with patch.object(Teammate, "start"):
            runtime.teams.spawn("writer", "implementer", "Implement parser.py.")
        teammate = runtime.teams.get("writer")
        teammate.claim_task(task.id)

        teammate._run_turn()

        self.assertFalse((self.tmp / "parser.py").exists())
        self.assertEqual(runtime.tasks.load(task.id).status, IN_PROGRESS)
        self.assertEqual(runtime.teams.protocol.plan_gates["writer"], "pending")
        self.assertIn("Blocked: plan status is pending", str(teammate.messages))
        events = runtime.teams.consume_lead_inbox()
        self.assertIn("submitted a plan", events[0])
        request_id = runtime.teams.protocol.plan_request_ids["writer"]
        self.assertIn(request_id, events[0])

        runtime.execute_tool({
            "type": "tool_use", "id": "review", "name": "review_plan",
            "input": {"request_id": request_id, "approve": True, "feedback": "Proceed."},
        })
        self.assertTrue(teammate._queue_inbox())
        teammate._run_turn()

        self.assertTrue((self.tmp / "parser.py").exists(), str(teammate.messages))
        self.assertEqual((self.tmp / "parser.py").read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(runtime.tasks.load(task.id).status, COMPLETED)
        self.assertNotIn("request_plan", self.called_tools(runtime))

    def test_lead_delegates_and_the_teammate_claims_and_finishes(self):
        state = {"spawned": False, "teammate_turns": 0}

        def responder(index, messages, tools):
            names = {tool.get("name") for tool in tools}
            if "spawn_teammate" in names:
                if not state["spawned"]:
                    state["spawned"] = True
                    return {
                        "text": "Splitting this out.",
                        "tool": "spawn_teammate",
                        "input": {
                            "name": "writer",
                            "role": "implementer",
                            "prompt": "Complete the task titled 'Write parser.py'.",
                        },
                    }
                return "Delegated to writer."

            # ---- teammate ----
            state["teammate_turns"] += 1
            found = re.search(r"task_[0-9a-f]{8}", self._all_text(messages))
            if found:
                return {"tool": "complete_task", "input": {"task_id": found.group(0)}}
            return "Checking the board."

        runtime = self.make_runtime(responder=responder)
        runtime._emit = lambda text: None  # keep test output clean

        task = runtime.tasks.create("Write parser.py", "Implement the tokenizer.")

        answer = runtime.submit("Delegate writing parser.py to a teammate")
        self.assertEqual(answer, "Delegated to writer.")
        self.assertTrue(state["spawned"])

        teammate = runtime.teams.get("writer")
        self.assertIsNotNone(teammate)
        teammate.idle_rounds = 0  # retire as soon as there is nothing left

        deadline = time.monotonic() + 20.0
        while teammate.alive and time.monotonic() < deadline:
            time.sleep(0.05)

        self.assertFalse(teammate.alive, "teammate thread did not finish")
        stored = runtime.tasks.load(task.id)
        self.assertEqual(stored.status, COMPLETED)
        self.assertEqual(stored.owner, "writer")
        self.assertGreaterEqual(state["teammate_turns"], 2)
        self.assertIn("writer", runtime.teams.roster())

    def test_teammate_denied_a_write_asks_the_lead_instead_of_blocking(self):
        """A teammate is non-interactive, so a risky write fails closed."""
        runtime = self.make_runtime(approval="deny")
        teammate_entry = runtime.teams.spawn(
            "writer", "implementer", "Write outside the workspace.", autonomous=False
        )
        self.assertIn("Spawned", teammate_entry)
        teammate = runtime.teams.get("writer")
        teammate.idle_rounds = 0

        ctx = teammate._tool_context()
        self.assertFalse(ctx.interactive)
        decision = runtime.permissions.check(
            "write_file", {"path": "../outside.txt", "content": "x"},
            workdir=ctx.workdir, interactive=ctx.interactive,
        )
        self.assertFalse(decision.allowed)
        self.assertFalse((self.tmp.parent / "outside.txt").exists())

        runtime.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
