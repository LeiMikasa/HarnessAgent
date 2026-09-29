"""Compare the same coding task with context compaction on and off.

This is a controlled task-level pilot. Mock mode verifies the measurement
pipeline; only repeated real-model runs can support a quality or latency claim.
No token or price estimates are inferred from character counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from agent.config import load_settings
from agent.llm import LLMClient, LLMResponse, build_client, tool_use_blocks
from agent.permissions import DENY
from agent.runtime import Runtime
from evals.verification_feedback import FIXTURE, MOCK_REPAIRED, TASK_PROMPT, verify

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / ".scratch" / "evals"
ALLOWED_TOOLS = {"read_file", "write_file", "edit_file", "glob", "grep"}
RETENTION_PROMPT = (
    "Earlier history contained a RELEASE_CODE value. Find its exact "
    "value in the conversation or an archived transcript, then write only that "
    "value to answer.txt. Do not guess, and do not edit other files."
)


def diagnostic_output(index: int, target_chars: int, release_code: str | None = None) -> str:
    """Stable, code-like noise; identical in both arms of a paired trial."""
    lines: list[str] = [f"RELEASE_CODE={release_code}\n"] if release_code else []
    size = sum(map(len, lines))
    row = 0
    while size < target_chars:
        digest = hashlib.sha256(f"context-eval:{index}:{row}".encode()).hexdigest()[:12]
        line = (
            f"trace shard={index:02d} row={row:04d} module=diagnostics.worker "
            f"status=ok checksum={digest} detail=unrelated historical tool output\n"
        )
        lines.append(line)
        size += len(line)
        row += 1
    return "".join(lines)[:target_chars]


def initial_history(
    pairs: int, result_chars: int, release_code: str | None = None,
) -> list[dict]:
    """Valid role alternation and paired tool_use/tool_result blocks."""
    history: list[dict] = []
    for index in range(pairs):
        tool_id = f"history_{index:03d}"
        history.extend(
            [
                {"role": "user", "content": f"Review historical diagnostic shard {index}."},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": tool_id, "name": "read_file",
                         "input": {"path": f"diagnostics/shard_{index:03d}.log"}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tool_id,
                         "content": diagnostic_output(
                             index, result_chars, release_code if index == 4 else None
                         )}
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": "Reviewed."}]},
            ]
        )
    return history


def summarization_history(release_code: str) -> list[dict]:
    """Long ordinary text forces model summarization after cheaper stages fail."""
    return [
        {"role": "user", "content": (
            f"Historical release note: RELEASE_CODE={release_code}\n"
            + diagnostic_output(900, 90_000)
        )},
        {"role": "assistant", "content": [{"type": "text", "text": "Noted."}]},
    ]


class FixtureMockLLM:
    """Offline plumbing driver; it does not model real reasoning quality."""

    provider = "mock"
    model = "context-fixture-mock-1"

    def __init__(self, case: str) -> None:
        self.case = case
        self.step = 0

    @staticmethod
    def visible_text(messages: list[dict]) -> str:
        parts: list[str] = []
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(str(block.get("content", "")) for block in content
                             if isinstance(block, dict))
        return "\n".join(parts)

    def create(self, **kwargs: Any) -> LLMResponse:
        if not kwargs["tools"]:  # compactor's summarization request
            text = self.visible_text(kwargs["messages"])
            code = re.search(r"RELEASE_CODE=([A-Z0-9-]+)", text)
            return LLMResponse(
                content=[{"type": "text", "text": (
                    f"Preserve RELEASE_CODE={code.group(1)}" if code else
                    "Historical diagnostics are unrelated to the CSV task."
                )}],
                stop_reason="end_turn",
            )
        if self.case in ("retention", "summarization"):
            text = self.visible_text(kwargs["messages"])
            code = re.search(r"RELEASE_CODE=([A-Z0-9-]+)", text)
            if code and self.step < 2:
                self.step = 2
                return LLMResponse(
                    content=[{"type": "tool_use", "id": "mock_answer_1", "name": "write_file",
                              "input": {"path": "answer.txt", "content": code.group(1)}}],
                    stop_reason="tool_use",
                )
            if self.step == 0:
                archive = re.search(r"messages archived at ([^\]\n]+)", text)
                transcript = re.search(r"Full transcript: ([^\n]+)", text)
                path = archive.group(1) if archive else (
                    transcript.group(1) if transcript else None
                )
                if path:
                    self.step = 1
                    return LLMResponse(
                        content=[{"type": "tool_use", "id": "mock_find_1", "name": "grep",
                                  "input": {"path": path.strip(), "pattern": "RELEASE_CODE="}}],
                        stop_reason="tool_use",
                    )
            return LLMResponse(
                content=[{"type": "text", "text": "Done if the code was found."}],
                stop_reason="end_turn",
            )
        if self.step == 0:
            self.step += 1
            return LLMResponse(
                content=[{"type": "tool_use", "id": "mock_write_1", "name": "write_file",
                          "input": {"path": "csv_line.py", "content": MOCK_REPAIRED}}],
                stop_reason="tool_use",
            )
        return LLMResponse(content=[{"type": "text", "text": "Implemented."}], stop_reason="end_turn")


@dataclass
class MeasuredLLM:
    inner: LLMClient
    calls: int = 0
    summary_calls: int = 0
    tool_calls: int = 0
    context_errors: int = 0
    input_chars: list[int] = field(default_factory=list)
    tool_definition_sha256: str | None = None
    model_seconds: float = 0.0

    @property
    def provider(self) -> str:
        return self.inner.provider

    @property
    def model(self) -> str:
        return self.inner.model

    def create(self, **kwargs: Any) -> LLMResponse:
        if kwargs["tools"] and self.tool_definition_sha256 is None:
            self.tool_definition_sha256 = hashlib.sha256(json.dumps(
                kwargs["tools"], ensure_ascii=False, sort_keys=True, default=str
            ).encode("utf-8")).hexdigest()
        payload = (str(kwargs["system"]) + json.dumps(
            kwargs["messages"], ensure_ascii=False, default=str
        ) + json.dumps(kwargs["tools"], ensure_ascii=False, default=str))
        self.input_chars.append(len(payload))
        self.calls += 1
        if not kwargs["tools"]:
            self.summary_calls += 1
        started = time.perf_counter()
        try:
            response = self.inner.create(**kwargs)
        except Exception as exc:
            message = str(exc).lower()
            if any(marker in message for marker in (
                "prompt_too_long", "too many tokens", "context_length", "maximum context"
            )):
                self.context_errors += 1
            raise
        finally:
            self.model_seconds += time.perf_counter() - started
        self.tool_calls += len(tool_use_blocks(response.content))
        return response


def run_worker(
    case: str, mode: str, run_dir: Path, provider: str, model: str | None,
    pairs: int, result_chars: int, seed: int,
) -> dict[str, Any]:
    workspace = run_dir / "workspace"
    if case == "coding":
        shutil.copytree(FIXTURE, workspace)
    else:
        workspace.mkdir()
    settings = load_settings(
        provider=provider, model=model or ("mock-1" if provider == "mock" else None),
        workdir=workspace, state_dir=workspace / ".agent", skill_dirs=[],
        max_turns=16, max_tokens=4000,
    )
    if provider != "mock" and not settings.api_key:
        raise RuntimeError("API key is missing; configure .env before live evaluation")
    llm = MeasuredLLM(FixtureMockLLM(case) if provider == "mock" else build_client(settings))
    started = time.perf_counter()
    events: list[str] = []
    result: dict[str, Any] = {
        "case": case, "mode": mode, "provider": provider, "model": settings.model,
        "workspace": str(workspace), "pairs": pairs, "result_chars": result_chars,
        "status": "running", "success": False, "verifier": "not run",
        "compaction_enabled": mode == "compact",
    }
    runtime: Runtime | None = None
    try:
        runtime = Runtime(
            settings, llm, teams_enabled=False, memory_enabled=False,
            compaction_enabled=(mode == "compact"), approval=DENY,
            on_event=events.append,
        )
        # Keep model-visible tools identical in both arms. The experiment
        # evaluates automatic compression, not the model's use of `compact`.
        for name in runtime.tools.names():
            if name not in ALLOWED_TOOLS:
                runtime.tools.unregister(name)
        release_code = (
            hashlib.sha256(f"context-retention:{seed}".encode()).hexdigest()[:8].upper()
            if case != "coding" else None
        )
        history = (
            summarization_history(release_code) if case == "summarization" else
            initial_history(pairs, result_chars, release_code)
        )
        result["initial_history_sha256"] = hashlib.sha256(json.dumps(
            history, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")).hexdigest()
        runtime.messages.extend(history)
        result["initial_history_chars"] = runtime.compactor.estimate_chars(runtime.messages)
        answer = runtime.submit(TASK_PROMPT if case == "coding" else RETENTION_PROMPT)
        if case == "coding":
            passed, detail = verify(workspace)
        else:
            answer_path = workspace / "answer.txt"
            passed = answer_path.is_file() and answer_path.read_text(encoding="utf-8") == release_code
            detail = "exact historical value recovered" if passed else "historical value missing or incorrect"
        result["verifier"] = detail
        result["answer_finished"] = not answer.startswith(("(error:", "(max_turns:"))
        result["status"] = "success" if passed and result["answer_finished"] else (
            "verification_failed" if not passed else "agent_error"
        )
        result["final_history_chars"] = runtime.compactor.estimate_chars(runtime.messages)
        result["lead_turns"] = runtime.stats.turns
        result["archive_files"] = len(list(settings.transcripts_dir.glob("*.jsonl")))
        result["tool_result_files"] = len(list((settings.state_dir / "tool-results").glob("*")))
        result["compaction_triggered"] = bool(
            result["archive_files"] or result["tool_result_files"] or llm.summary_calls
        )
        result["compaction_events"] = [
            event for event in events if event.startswith((
                "archived ", "micro-compacted ", "transcript saved ",
                "history compacted ", "reactive compaction ", "[reactive compact]",
            ))
        ][:30]
    except Exception as exc:  # one failed trial must not erase its counterpart
        result["status"] = "runtime_error"
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if runtime is not None:
            runtime.close()
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        result["model_calls"] = llm.calls
        result["summary_calls"] = llm.summary_calls
        result["tool_calls"] = llm.tool_calls
        result["context_errors"] = llm.context_errors
        result["input_chars_sum"] = sum(llm.input_chars)
        result["input_chars_max"] = max(llm.input_chars, default=0)
        result["input_chars_by_call"] = llm.input_chars
        result["tool_definition_sha256"] = llm.tool_definition_sha256
        result["model_seconds_sum"] = round(llm.model_seconds, 3)
        result["success"] = result["status"] == "success"
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result


def run_parent(args: argparse.Namespace) -> int:
    settings = load_settings(
        provider=args.provider,
        model=args.model or ("mock-1" if args.provider == "mock" else None),
    )
    if args.provider != "mock" and not settings.api_key:
        print("Missing API key in .env; no paid runs started.", file=sys.stderr)
        return 2
    batch = (args.output / (
        f"context-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
        f"{args.provider}-{uuid.uuid4().hex[:6]}"
    )).resolve()
    batch.mkdir(parents=True, exist_ok=False)
    modes = ["full", "compact"] if args.mode == "both" else [args.mode]
    cases = ["coding", "retention", "summarization"] if args.case == "all" else (
        ["coding", "retention"] if args.case == "both" else [args.case]
    )
    results: list[dict[str, Any]] = []
    for repeat in range(1, args.repeats + 1):
        for case_index, case in enumerate(cases):
            order = modes if (repeat + case_index) % 2 else list(reversed(modes))
            for mode in order:
                run_dir = batch / f"repeat-{repeat:02d}-{case}-{mode}"
                run_dir.mkdir()
                print(f"[{repeat}/{args.repeats}] {case}/{mode}: running...", flush=True)
                command = [
                    sys.executable, "-m", "evals.context_compaction", "--worker",
                    "--provider", args.provider, "--case", case, "--mode", mode,
                    "--run-dir", str(run_dir), "--pairs", str(args.pairs),
                    "--result-chars", str(args.result_chars), "--seed", str(repeat),
                ]
                if args.model:
                    command += ["--model", args.model]
                started = time.perf_counter()
                try:
                    completed = subprocess.run(
                        command, cwd=str(ROOT), capture_output=True, text=True,
                        errors="replace", timeout=args.timeout,
                    )
                except subprocess.TimeoutExpired:
                    record = {"case": case, "mode": mode, "repeat": repeat,
                              "status": "hard_timeout", "success": False,
                              "elapsed_seconds": round(time.perf_counter() - started, 3)}
                else:
                    path = run_dir / "result.json"
                    if path.is_file():
                        record = json.loads(path.read_text(encoding="utf-8"))
                        record["repeat"] = repeat
                    else:
                        record = {"case": case, "mode": mode, "repeat": repeat,
                                  "status": "worker_crashed", "success": False,
                                  "elapsed_seconds": round(time.perf_counter() - started, 3),
                                  "stderr": completed.stderr[-500:]}
                results.append(record)
                print(
                    f"[{repeat}/{args.repeats}] {case}/{mode}: {record['status']}; "
                    f"calls={record.get('model_calls', '?')}, "
                    f"max input chars={record.get('input_chars_max', '?')}", flush=True,
                )
    aggregate: dict[str, Any] = {}
    for case in cases:
        aggregate[case] = {}
        for mode in modes:
            arm = [r for r in results if r["case"] == case and r["mode"] == mode]
            successful = [r for r in arm if r["success"]]
            aggregate[case][mode] = {
                "successes": len(successful), "trials": len(arm),
                "context_errors": sum(r.get("context_errors", 0) for r in arm),
                "median_success_seconds": median(r["elapsed_seconds"] for r in successful)
                if successful else None,
                "median_input_chars_sum": median(r["input_chars_sum"] for r in arm
                                          if "input_chars_sum" in r)
                if all("input_chars_sum" in r for r in arm) else None,
            }
    pairs = []
    for repeat in range(1, args.repeats + 1):
        for case in cases:
            full = next((r for r in results if r["repeat"] == repeat
                         and r["case"] == case and r["mode"] == "full"), None)
            compact = next((r for r in results if r["repeat"] == repeat
                            and r["case"] == case and r["mode"] == "compact"), None)
            if full and compact and all("initial_history_sha256" in r for r in (full, compact)):
                if full["initial_history_sha256"] != compact["initial_history_sha256"]:
                    raise RuntimeError(f"History differs between arms: {case}, repeat {repeat}")
                if full.get("tool_definition_sha256") != compact.get("tool_definition_sha256"):
                    raise RuntimeError(f"Tool definitions differ between arms: {case}, repeat {repeat}")
            if full and compact and full["success"] and compact["success"]:
                pairs.append({
                    "repeat": repeat, "case": case,
                    "full_seconds": full["elapsed_seconds"],
                    "compact_seconds": compact["elapsed_seconds"],
                    "full_input_chars": full["input_chars_sum"],
                    "compact_input_chars": compact["input_chars_sum"],
                })
    summary = {
        "cases": cases, "provider": args.provider,
        "model": args.model or settings.model, "repeats": args.repeats,
        "pairs_of_history": args.pairs, "chars_per_result": args.result_chars,
        "comparison": "same task, model, tool pool, history, and budget; only automatic compaction differs",
        "primary_outcome": "external CSV acceptance, not model self-report",
        "measurement_note": "Input characters include system, message, and tool-definition JSON; they are not tokens or cost. Mock validates plumbing only.",
        "aggregate": aggregate, "comparable_pairs": pairs, "results": results,
    }
    path = batch / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Summary: {path}", flush=True)
    if args.mode in ("both", "compact") and not any(
        r.get("compaction_triggered") for r in results if r["mode"] == "compact"
    ):
        print("Warning: compression did not trigger; increase --pairs or --result-chars.",
              file=sys.stderr)
    return 1 if any(r["status"] in {"runtime_error", "worker_crashed", "hard_timeout"}
                    for r in results) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("mock", "deepseek", "anthropic"), default="mock")
    parser.add_argument("--model")
    parser.add_argument("--case", choices=("both", "all", "coding", "retention", "summarization"),
                        default="both")
    parser.add_argument("--mode", choices=("both", "full", "compact"), default="both")
    parser.add_argument("--pairs", type=int, default=28,
                        help="historical tool-result pairs; 13 or more can trigger the message-count stage")
    parser.add_argument("--result-chars", type=int, default=5000,
                        help="characters in each historical tool result")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=240.0,
                        help="hard timeout for each trial in seconds")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--seed", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1 or args.pairs < 1 or args.result_chars < 1 or args.timeout <= 0:
        parser.error("repeats, pairs, result-chars and timeout must be positive")
    if args.case in ("both", "all", "retention") and (args.pairs < 5 or args.result_chars < 64):
        parser.error("retention case needs at least 5 pairs and 64 chars per result")
    if args.worker:
        if args.run_dir is None or args.mode == "both" or args.case in ("both", "all"):
            parser.error("worker requires --run-dir, one case and one mode")
        run_worker(args.case, args.mode, args.run_dir, args.provider, args.model,
                   args.pairs, args.result_chars, args.seed)
        return 0
    return run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
