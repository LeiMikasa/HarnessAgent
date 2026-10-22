"""Evaluation-only Codex CLI transport; minicode keeps control of all tools.

Each request starts a fresh ephemeral Codex session in an empty directory.
Tool requests are JSON in the final message, never Codex's built-in tools.
CLI token usage includes its system/transport overhead and is not an API bill.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from agent.llm import LLMResponse
from agent.shell import noninteractive_process_options


RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "text": {"type": "string"},
        "tool_calls": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string"}, "arguments_json": {"type": "string"}},
            "required": ["name", "arguments_json"],
        }},
    },
    "required": ["text", "tool_calls"],
}

PROTOCOL = """Act as the language-model component of minicode, not as a separate coding agent.
The JSON request below contains the ONLY conversation, system instructions and tools to use.
Return one next assistant turn conforming to the supplied response schema.
To call a supplied tool, put its exact name and JSON-encoded input object in tool_calls.
Do NOT execute any built-in tools, browse files yourself, or access the evaluator or answer keys.
The host will execute requested tools and send results in a subsequent fresh request.
For a final answer, return text and an empty tool_calls array. With no supplied tools, write
only the requested summary in text. Do not infer facts from prior sessions or outside context.
"""


class CodexLLM:
    provider = "codex"

    def __init__(self, model: str | None, transport_dir: Path, *, timeout: float = 180):
        self.model = model or "codex-default"
        self.executable = shutil.which("codex")
        if not self.executable:
            raise RuntimeError("Codex CLI was not found on PATH")
        self.transport_dir = Path(transport_dir)
        self.transport_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.call = 0
        self.deadline = None
        self.transport_events = []

    def create(self, *, system, messages, tools, max_tokens, **kwargs):
        self.call += 1
        call_dir = self.transport_dir / f"call-{self.call:03d}"
        call_dir.mkdir(exist_ok=False)
        empty_cwd = call_dir / "empty"
        empty_cwd.mkdir()
        schema = call_dir / "response.schema.json"
        schema.write_text(json.dumps(RESPONSE_SCHEMA), encoding="utf-8")
        payload = {"system": system, "messages": messages, "tools": tools,
                   "requested_output_tokens": max_tokens}
        prompt = PROTOCOL + "\nREQUEST_JSON:\n" + json.dumps(payload, ensure_ascii=False)
        (call_dir / "request.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        command = [self.executable, "exec", "--ignore-user-config", "--ephemeral",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--json",
                   "--color", "never", "--cd", str(empty_cwd),
                   "--output-schema", str(schema),
                   "-c", 'web_search="disabled"',
                   "-c", "features.shell_tool=false", "-c", "features.multi_agent=false",
                   "-c", "features.plugins=false", "-c", "features.hooks=false",
                   "-c", "features.memories=false", "-c", "features.sleep_tool=false"]
        if self.model != "codex-default":
            command.extend(["--model", self.model])
        command.append("-")
        # Never let API-key environment overrides change ChatGPT-plan auth.
        env = {k: v for k, v in os.environ.items() if k not in {"CODEX_API_KEY", "OPENAI_API_KEY"}}
        limit = self.timeout
        if self.deadline is not None:
            limit = min(limit, self.deadline - time.monotonic())
        if limit <= 0:
            raise TimeoutError("Evaluation run deadline reached")
        try:
            process = subprocess.run(
                command, input=prompt, text=True, encoding="utf-8", errors="replace",
                capture_output=True, env=env, timeout=limit,
                **noninteractive_process_options(input_provided=True))
        except subprocess.TimeoutExpired as exc:
            # On Windows subprocess.run kills only the CLI process. CLI-owned tool
            # calls are disabled; no minicode tools are running at this boundary.
            raise TimeoutError("Codex request timed out; usage was not fully reported") from exc
        (call_dir / "events.jsonl").write_text(process.stdout, encoding="utf-8")
        (call_dir / "stderr.log").write_text(process.stderr, encoding="utf-8")
        if process.returncode:
            raise RuntimeError(f"Codex CLI exited {process.returncode}: {process.stderr[-600:]}")
        return self.parse_output(process.stdout, tools)

    def parse_output(self, output: str, tools: list) -> LLMResponse:
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
        completed = [e for e in events if e.get("type") == "turn.completed"]
        if len(completed) != 1 or any(e.get("type") in {"turn.failed", "error"} for e in events):
            raise RuntimeError("Codex did not report exactly one completed turn")
        items = [e.get("item", {}) for e in events if e.get("type", "").startswith("item.")]
        if any(i.get("type") not in {"agent_message", "reasoning"} for i in items):
            raise RuntimeError("Codex used a built-in tool; this trial is invalid")
        answers = [e["item"]["text"] for e in events if e.get("type") == "item.completed"
                   and e.get("item", {}).get("type") == "agent_message"]
        if not answers:
            raise RuntimeError("Codex returned no structured assistant message")
        answer = json.loads(answers[-1])
        if (set(answer) != {"text", "tool_calls"} or not isinstance(answer["text"], str)
                or not isinstance(answer["tool_calls"], list)):
            raise ValueError("Invalid assistant envelope")
        content = [{"type": "text", "text": answer["text"]}] if answer["text"] else []
        names = {tool["name"] for tool in tools}
        for call in answer["tool_calls"]:
            if call.get("name") not in names:
                raise ValueError("Codex requested a tool not offered by minicode")
            args = json.loads(call["arguments_json"])
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object")
            content.append({"type": "tool_use", "id": "codex_" + uuid.uuid4().hex,
                            "name": call["name"], "input": args})
        raw_usage = completed[0].get("usage")
        usage = None
        if isinstance(raw_usage, dict):
            usage = {**raw_usage, "raw_codex_usage": dict(raw_usage),
                     "transport": "codex-cli", "includes_cli_overhead": True}
            if "cached_input_tokens" in raw_usage:
                usage["cache_read_input_tokens"] = raw_usage["cached_input_tokens"]
        self.transport_events.append({"call": self.call, "usage": usage})
        return LLMResponse(content, "tool_use" if answer["tool_calls"] else "end_turn", usage)
