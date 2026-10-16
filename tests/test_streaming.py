"""SSE fixtures exercise the real adapter without network or model charges."""

import threading
from types import SimpleNamespace as NS

from common import HarnessCase, make_settings
from agent.llm import AnthropicLLM, LLMError, MockLLM
from agent.loop import run_loop
from agent.tools.registry import ToolContext, ToolRegistry


class StreamFixture:
    def __init__(self, events, message):
        self.events, self.message = events, message
        self.final_read = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def __iter__(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event

    def get_final_message(self):
        self.final_read = True
        return self.message


def delta(kind, **data):
    return NS(type="content_block_delta", delta=NS(type=kind, **data))


def adapter(*streams):
    client = AnthropicLLM.__new__(AnthropicLLM)
    client.model, client.provider = "fixture", "fixture"
    remaining = iter(streams)
    client._client = NS(messages=NS(stream=lambda **kwargs: next(remaining)))
    return client


def response(content, stop="end_turn"):
    return {"content": content, "stop_reason": stop,
            "usage": {"input_tokens": 10, "output_tokens": 7}}


class StreamingTests(HarnessCase):
    def drive(self, llm, registry=None, cancel=None):
        events, messages = [], [{"role": "user", "content": "go"}]
        ctx = ToolContext(settings=make_settings(self.tmp), workdir=self.tmp,
                          extra={"ui_events": events.append, "cancel_event": cancel})
        result = run_loop(llm=llm, registry=registry or ToolRegistry(), messages=messages,
                          system="sys", ctx=ctx, max_turns=6)
        return result, messages, events

    def test_text_stream_excludes_thinking_and_partial_tool_json_and_keeps_usage(self):
        tool = {"type": "tool_use", "id": "t", "name": "write_file", "input": {"path": "x", "content": "y"}}
        stream = StreamFixture([
            delta("thinking_delta", thinking="private reasoning"),
            delta("text_delta", text="公开"), delta("text_delta", text="说明"),
            delta("input_json_delta", partial_json='{"path":"x"'), NS(type="message_stop"),
        ], response([{"type": "text", "text": "公开说明"}, tool], "tool_use"))
        chunks = []
        result = adapter(stream).create(system="sys", messages=[], tools=[], on_text=chunks.append)
        self.assertEqual(chunks, ["公开", "说明"])
        self.assertEqual(result.content[1]["input"], {"path": "x", "content": "y"})
        self.assertEqual(result.usage["output_tokens"], 7)
        self.assertTrue(stream.final_read and stream.closed)

    def test_eof_before_message_stop_never_accepts_snapshot_or_executes_tools(self):
        executed = []
        registry = ToolRegistry()
        registry.add("write", "", {}, lambda a, c: executed.append(True))
        tool = {"type": "tool_use", "id": "t", "name": "write", "input": {}}
        stream = StreamFixture([delta("text_delta", text="还没完成")], response([tool], "tool_use"))
        result, messages, events = self.drive(adapter(stream), registry)
        self.assertEqual(result.stop_reason, "error")
        self.assertEqual(executed, [])
        self.assertEqual(len(messages), 1)
        self.assertFalse(stream.final_read)
        self.assertTrue(any(e.kind == "model_failed" for e in events))

    def test_stream_exception_does_not_replay_or_execute_partial_tool_call(self):
        stream = StreamFixture([delta("text_delta", text="部分文字"), ConnectionError("broken")], response([]))
        result, messages, events = self.drive(adapter(stream))
        self.assertEqual(result.stop_reason, "error")
        self.assertEqual(len(messages), 1)
        self.assertEqual([e.text for e in events if e.kind == "text_delta"], ["部分文字"])
        self.assertTrue(stream.closed)

    def test_complete_stream_with_truncated_tool_call_retains_existing_recovery(self):
        executed = []
        registry = ToolRegistry()
        registry.add("write", "", {}, lambda a, c: executed.append(True))
        tool = {"type": "tool_use", "id": "t", "name": "write", "input": {}}
        first = StreamFixture([NS(type="message_stop")], response([tool], "max_tokens"))
        second = StreamFixture([delta("text_delta", text="已改用说明"), NS(type="message_stop")],
                               response([{"type": "text", "text": "已改用说明"}]))
        result, messages, _ = self.drive(adapter(first, second), registry)
        self.assertTrue(result.ok)
        self.assertEqual(executed, [])
        self.assertNotIn("tool_use", str(messages))

    def test_stream_cancellation_closes_connection_without_accepting_tools(self):
        stop = threading.Event()
        stream = StreamFixture([delta("text_delta", text="部分"), NS(type="message_stop")], response([]))
        with self.assertRaises(LLMError):
            adapter(stream).create(system="sys", messages=[], tools=[], on_text=lambda text: stop.set(),
                                   should_cancel=stop.is_set)
        self.assertTrue(stream.closed)
        self.assertFalse(stream.final_read)

    def test_nonstreaming_api_is_preserved(self):
        llm = adapter()
        llm._client.messages.create = lambda **kwargs: response([{"type": "text", "text": "whole"}])
        self.assertEqual(llm.create(system="sys", messages=[], tools=[]).text(), "whole")

    def test_mock_stream_only_emits_public_text(self):
        mock = MockLLM(script=[[{"type": "thinking", "thinking": "private"}, {"text": "公开文字"}]])
        chunks = []
        result = mock.create(system="sys", messages=[], tools=[], on_text=chunks.append)
        self.assertEqual("".join(chunks), result.text())
        self.assertNotIn("private", "".join(chunks))

    def test_cancel_between_tools_keeps_pairing_and_skips_remaining_effects(self):
        stop, executed = threading.Event(), []
        registry = ToolRegistry()

        def first(args, ctx):
            stop.set()
            return "first completed"

        registry.add("first", "", {}, first)
        registry.add("second", "", {}, lambda a, c: executed.append(True))
        llm = MockLLM(script=[[{"tool": "first", "input": {}}, {"tool": "second", "input": {}}]])
        result, messages, _ = self.drive(llm, registry, stop)
        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(executed, [])
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(messages[-1]["content"]), 2)
        self.assertTrue(messages[-1]["content"][1]["is_error"])

    def test_chunk_count_does_not_multiply_token_or_call_totals(self):
        from agent.diagnostics import ObservedLLM, RunTrace
        stream = StreamFixture([delta("text_delta", text="a"), delta("text_delta", text="b"),
                                NS(type="message_stop")], response([{"type": "text", "text": "ab"}]))
        trace = RunTrace(self.tmp / "runs", provider="fixture", model="fixture")
        client = ObservedLLM(adapter(stream), trace, "agent")
        client.create(system="sys", messages=[], tools=[], on_text=lambda text: None)
        summary = trace.summary()
        self.assertEqual(summary["counts"]["model_calls"], 1)
        self.assertEqual(summary["token_totals"]["input_tokens"], 10)
        self.assertEqual(summary["token_totals"]["output_tokens"], 7)
