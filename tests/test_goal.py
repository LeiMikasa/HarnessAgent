"""Goal loop behavior, including the independent judge boundary."""

from __future__ import annotations

import unittest

from agent.goal import GoalController, GoalEvaluation, PromptGoalEvaluator, parse_evaluation, render_evidence
from agent.llm import MockLLM
from tests.common import HarnessCase, make_settings


class SequenceEvaluator:
    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.calls = []

    def evaluate(self, condition, messages):
        self.calls.append((condition, render_evidence(messages)))
        verdict = self.verdicts.pop(0)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict


class GoalTests(HarnessCase):
    def test_unmet_goal_continues_and_then_completes(self):
        judge = SequenceEvaluator(
            GoalEvaluation(False, "test has not run"),
            GoalEvaluation(True, "test output confirms success"),
        )
        runtime = self.make_runtime(script=["Done.", "Test passed."], goal_evaluator=judge)
        answer = runtime.goal_command("Run the test successfully")
        self.assertIn("[goal: achieved]", answer)
        self.assertEqual(len(runtime.llm.calls), 2)
        self.assertIn("test has not run", str(runtime.llm.calls[1]["messages"]))
        self.assertEqual(runtime.goal.summary(), "achieved: test output confirms success")
        self.assertEqual(runtime.submit("Another request"), "Done.")

    def test_judge_sees_tool_result(self):
        judge = SequenceEvaluator(GoalEvaluation(True, "file verified"))
        runtime = self.make_runtime(
            script=[{"tool": "read_file", "input": {"path": "x.txt"}}, "Read it."],
            goal_evaluator=judge,
        )
        self.write("x.txt", "evidence-marker")
        runtime.goal_command("Read x.txt")
        self.assertIn("evidence-marker", judge.calls[0][1])

    def test_failure_clears_goal_and_error_preserves_it(self):
        judge = SequenceEvaluator(GoalEvaluation(False, "cannot be done", True))
        runtime = self.make_runtime(script=["Unable."], goal_evaluator=judge)
        self.assertIn("[goal: failed]", runtime.goal_command("Do impossible thing"))
        self.assertFalse(runtime.goal.active)

        error_judge = SequenceEvaluator(RuntimeError("service unavailable"))
        runtime2 = self.make_runtime(script=["Done."], goal_evaluator=error_judge)
        self.assertIn("[goal: error]", runtime2.goal_command("Do something"))
        self.assertTrue(runtime2.goal.active)

    def test_cap_preserves_goal_and_clear(self):
        judge = SequenceEvaluator(*(GoalEvaluation(False, "still missing") for _ in range(3)))
        settings = make_settings(self.tmp, goal_block_cap=1)
        runtime = self.make_runtime(settings=settings, script=["A", "B"], goal_evaluator=judge)
        self.assertIn("[goal: limit]", runtime.goal_command("Reach condition"))
        self.assertTrue(runtime.goal.active)
        self.assertEqual(runtime.goal_command(), runtime.goal.summary())
        self.assertEqual(runtime.goal_command("clear"), "Goal cleared.")
        self.assertFalse(runtime.goal.active)

    def test_no_goal_does_not_call_judge(self):
        judge = SequenceEvaluator()
        runtime = self.make_runtime(script=["Normal result"], goal_evaluator=judge)
        self.assertEqual(runtime.submit("Ordinary request"), "Normal result")
        self.assertFalse(judge.calls)

    def test_subagent_messages_do_not_trigger_lead_goal(self):
        judge = SequenceEvaluator()
        runtime = self.make_runtime(goal_evaluator=judge)
        runtime.goal.set("Lead task complete")
        self.assertIsNone(runtime._goal_stop_hook([{"role": "assistant", "content": "subagent done"}]))
        self.assertFalse(judge.calls)

    def test_replacement_and_input_limits(self):
        controller = GoalController(SequenceEvaluator())
        controller.set("First")
        controller.set("Second")
        self.assertEqual(controller.condition, "Second")
        with self.assertRaises(ValueError):
            controller.set("x" * 4001)
        self.assertEqual(controller.condition, "Second")

    def test_deferred_judgement_keeps_goal(self):
        judge = SequenceEvaluator()
        controller = GoalController(judge)
        controller.set("All tasks complete")
        self.assertEqual(controller.judge([], background_running=True), "defer")
        self.assertTrue(controller.active)
        self.assertFalse(judge.calls)

    def test_separate_model_has_no_tools(self):
        llm = MockLLM(script=['{"ok": true, "reason": "verified", "impossible": false}'])
        result = PromptGoalEvaluator(llm).evaluate("Condition", [{"role": "user", "content": "Evidence"}])
        self.assertTrue(result.ok)
        self.assertEqual(llm.calls[0]["tools"], [])
        self.assertEqual(llm.calls[0]["max_tokens"], 512)

    def test_rejects_malformed_verdict(self):
        with self.assertRaises(ValueError):
            parse_evaluation('{"ok": "yes", "reason": "fine", "impossible": false}')
        with self.assertRaises(ValueError):
            parse_evaluation('{"ok": true, "reason": "fine", "impossible": true}')


if __name__ == "__main__":
    unittest.main()
