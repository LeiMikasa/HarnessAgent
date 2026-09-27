"""Compare one agent with the persistent teammate workflow on the same task.

This is a task-level *pilot*, not a proof of speedup. The fixture and verifier
are fixed; every trial gets a fresh workspace and state directory. The timer
stops only after independent acceptance checks and teammate cleanup.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from agent.config import load_settings
from agent.llm import LLMClient, LLMResponse, build_client, tool_use_blocks
from agent.permissions import DENY
from agent.runtime import Runtime

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dual_modules"
VERIFIER = Path(__file__).resolve().parent / "verify_dual_modules.py"
DEFAULT_OUTPUT = ROOT / ".scratch" / "evals"

PROMPT = """Implement the four functions in numbers.py and strings.py. Use only
Python's standard library. Do not change function names or signatures.

Contract:
- median(values): numeric median; raise ValueError on empty input; do not
  mutate the input.
- moving_average(values, window): list of consecutive arithmetic means; a
  window larger than the input returns []; nonpositive window raises
  ValueError; do not mutate the input.
- slugify(text): Unicode NFKD normalization, strip diacritics to ASCII,
  lowercase, replace each run of non-[a-z0-9] characters with one hyphen,
  and strip leading/trailing hyphens.
- word_frequencies(text): count case-insensitive ASCII [a-z0-9]+ words in a
  dictionary; punctuation and underscores separate words.

The two files are independent. If persistent teammate tools are available,
create exactly one task for numbers.py, spawn one teammate to claim and finish
it, and implement strings.py yourself concurrently. If teammate tools are not
available, implement both files yourself. Do not use a short-lived subagent.
The external evaluator will run hidden acceptance checks. Do not edit tests.
"""


@dataclass
class CountedLLM:
    """Counts calls across lead and teammate threads, without logging prompts."""

    inner: LLMClient

    def __post_init__(self) -> None:
        self.provider = self.inner.provider
        self.model = self.inner.model
        self._lock = threading.Lock()
        self.calls = 0
        self.tool_calls = 0
        self.model_seconds = 0.0
        self.calls_by_role: Counter[str] = Counter()
        self.tool_names: Counter[str] = Counter()

    def create(self, **kwargs: Any) -> LLMResponse:
        role = "teammate" if threading.current_thread().name.startswith("teammate-") else "lead"
        started = time.perf_counter()
        try:
            response = self.inner.create(**kwargs)
        finally:
            elapsed = time.perf_counter() - started
            with self._lock:
                self.calls += 1
                self.calls_by_role[role] += 1
                self.model_seconds += elapsed
        with self._lock:
            calls = tool_use_blocks(response.content)
            self.tool_calls += len(calls)
            self.tool_names.update(str(call.get("name", "")) for call in calls)
        return response


NUMBERS_SOLUTION = '''"""Numeric utilities."""
def median(values):
    ordered = sorted(values)
    if not ordered:
        raise ValueError("empty input")
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2

def moving_average(values, window):
    if window <= 0:
        raise ValueError("window must be positive")
    return [sum(values[i:i + window]) / window
            for i in range(len(values) - window + 1)]
'''

STRINGS_SOLUTION = '''"""Text utilities."""
import re
import unicodedata

def slugify(text):
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")

def word_frequencies(text):
    counts = {}
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        counts[word] = counts.get(word, 0) + 1
    return counts
'''


class FixtureMockLLM:
    """Deterministic offline smoke driver; its timings are not results."""

    provider = "mock"
    model = "fixture-mock-1"

    def __init__(self, team_mode: bool):
        self.team_mode = team_mode
        self._lock = threading.Lock()
        self._steps: dict[str, int] = {}
        self._next_id = 0

    def create(self, **kwargs: Any) -> LLMResponse:
        role = "teammate" if threading.current_thread().name.startswith("teammate-") else "lead"
        with self._lock:
            step = self._steps.get(role, 0)
            self._steps[role] = step + 1
        messages = kwargs["messages"]
        task_id = re.search(r"task_[0-9a-f]{8}", str(messages))
        task_id = task_id.group(0) if task_id else ""
        if role == "teammate":
            if step == 0:
                calls = [("list_tasks", {})]
            elif step == 1:
                calls = [("claim_task", {"task_id": task_id})]
            elif step == 2:
                calls = [("write_file", {"path": "numbers.py", "content": NUMBERS_SOLUTION})]
            elif step == 3:
                calls = [("complete_task", {"task_id": task_id})]
            else:
                calls = []
        elif self.team_mode:
            if step == 0:
                calls = [("create_task", {"subject": "Implement numbers.py", "description":
                          "Implement median and moving_average per the user's contract."})]
            elif step == 1:
                calls = [("spawn_teammate", {"name": "numbers-worker", "role": "numeric utilities",
                          "prompt": "Claim the numbers.py task and implement both functions."}),
                         ("write_file", {"path": "strings.py", "content": STRINGS_SOLUTION})]
            else:
                calls = []
        elif step == 0:
            calls = [("write_file", {"path": "numbers.py", "content": NUMBERS_SOLUTION}),
                     ("write_file", {"path": "strings.py", "content": STRINGS_SOLUTION})]
        else:
            calls = []

        if not calls:
            return LLMResponse(content=[{"type": "text", "text": "Done."}], stop_reason="end_turn")
        with self._lock:
            blocks = []
            for name, arguments in calls:
                self._next_id += 1
                blocks.append({"type": "tool_use", "id": f"mock_{self._next_id}",
                               "name": name, "input": arguments})
        return LLMResponse(content=blocks, stop_reason="tool_use")


def verify(workspace: Path) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-I", str(VERIFIER), str(workspace)],
        cwd=str(workspace), capture_output=True, text=True, errors="replace", timeout=20,
    )
    detail = (result.stdout + result.stderr).strip()[:2000]
    return result.returncode == 0, detail


def task_statuses(runtime: Runtime) -> list[dict[str, str]]:
    return [{"id": task.id, "status": task.status, "owner": task.owner or ""}
            for task in runtime.tasks.list_all()]


def run_worker(mode: str, run_dir: Path, provider: str, model: str | None,
               timeout: float) -> dict[str, Any]:
    workspace = run_dir / "workspace"
    shutil.copytree(FIXTURE, workspace)
    settings = load_settings(
        provider=provider, model=model or ("mock-1" if provider == "mock" else None),
        workdir=workspace, state_dir=run_dir / "state", skill_dirs=[],
        max_turns=20, max_tokens=4000,
    )
    if provider != "mock" and not settings.api_key:
        raise RuntimeError("API key is missing; configure .env before live evaluation")
    team_mode = mode == "team"
    underlying = FixtureMockLLM(team_mode) if provider == "mock" else build_client(settings)
    llm = CountedLLM(underlying)
    started = time.perf_counter()
    result: dict[str, Any] = {
        "mode": mode, "provider": provider, "model": settings.model,
        "workspace": str(workspace), "status": "running", "success": False,
        "verifier": "not run", "teammates": [], "tasks": [],
        "verifier_runs": 0,
        "worktrees_enabled": False, "plan_gate_enabled": False,
        "memory_enabled": False, "compaction_enabled": False,
        "bash_enabled": False, "subagent_enabled": False,
    }
    runtime: Runtime | None = None
    try:
        runtime = Runtime(
            settings, llm, teams_enabled=team_mode, memory_enabled=False,
            compaction_enabled=False, require_plan=False, approval=DENY,
        )
        # Explicit allowlists avoid shell, MCP and short-lived subagents in
        # both arms, and make the available actions auditable.
        common = {"read_file", "write_file", "edit_file", "glob", "grep",
                  "todo_write", "list_tasks", "claim_task", "complete_task"}
        lead = common | {"create_task", "update_task", "get_task"}
        if team_mode:
            lead |= {"spawn_teammate", "list_teammates", "send_message"}
        teammate = common | {"send_message"}
        for registry, allowed in (
            (runtime.tools, lead),
            (runtime.teams.registry if runtime.teams else None, teammate),
        ):
            if registry is not None:
                for name in registry.names():
                    if name not in allowed:
                        registry.unregister(name)
        if runtime.teams:
            # This benchmark intentionally uses one shared, non-Git workspace.
            # Worktree changes have no automatic merge path in the current harness.
            runtime.teams.worktrees.enabled = False
        runtime.submit(PROMPT)
        deadline = started + timeout
        while time.perf_counter() < deadline:
            if runtime.teams:
                events = runtime.teams.consume_lead_inbox()
                if events:
                    runtime._inject(runtime.messages, runtime.teams.format_events(events))
                    runtime.run_turn()
            if time.perf_counter() >= deadline:
                result["status"] = "timeout"
                break

            tasks = task_statuses(runtime)
            teammates = list(runtime.teams.teammates.values()) if runtime.teams else []
            errors = [f"{t.name}: {t.state.error}" for t in teammates if t.state.error]
            if errors:
                result["status"] = "teammate_error"
                result["errors"] = errors
                break
            if team_mode and not tasks:
                result["status"] = "no_task"
                break

            all_tasks_done = all(task["status"] == "completed" for task in tasks)
            alive = any(t.alive for t in teammates)
            # Do not repeatedly launch the verifier while a teammate is still
            # implementing a claimed task. That would bias the team arm.
            if team_mode and alive and not (tasks and all_tasks_done):
                time.sleep(0.25)
                continue
            passed, detail = verify(workspace)
            result["verifier_runs"] += 1
            result["verifier"] = detail
            if passed and all_tasks_done and (not team_mode or (tasks and teammates)):
                result["status"] = "success"
                break
            if not alive:
                if team_mode and not teammates:
                    result["status"] = "no_delegation"
                elif not all_tasks_done:
                    result["status"] = "unfinished_task"
                else:
                    result["status"] = "verification_failed"
                break
            if all_tasks_done and not passed:
                result["status"] = "verification_failed"
                break
            time.sleep(0.25)
        else:
            result["status"] = "timeout"
    except Exception as exc:  # noqa: BLE001 - persist failure rather than losing a trial
        result["status"] = "runtime_error"
        result["error"] = f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        if runtime is not None:
            runtime.close()
            result["tasks"] = task_statuses(runtime)
            result["teammates"] = [
                {"name": t.name, "status": t.state.status, "alive": t.alive,
                 "error": t.state.error, "turns": t.state.turns}
                for t in (runtime.teams.teammates.values() if runtime.teams else [])
            ]
            result["lead_turns"] = runtime.stats.turns
        result["model_calls"] = llm.calls
        result["model_calls_by_role"] = dict(llm.calls_by_role)
        result["model_seconds_sum"] = round(llm.model_seconds, 3)
        result["tool_calls"] = llm.tool_calls
        result["tool_names"] = dict(llm.tool_names)
        # A successful result must still pass after teammate shutdown.
        if result["status"] == "success":
            passed, detail = verify(workspace)
            result["verifier_runs"] += 1
            result["verifier"] = detail
            if not passed or any(t["alive"] for t in result["teammates"]):
                result["status"] = "unstable_after_shutdown"
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        result["success"] = result["status"] == "success"
        result["team_used"] = bool(result["teammates"])
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result


def run_parent(args: argparse.Namespace) -> int:
    settings = load_settings(provider=args.provider,
                             model=args.model or ("mock-1" if args.provider == "mock" else None))
    if args.provider != "mock" and not settings.api_key:
        print("Missing API key in .env; no paid runs started.", file=sys.stderr)
        return 2
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_dir = (args.output / f"{timestamp}-{args.provider}-{uuid.uuid4().hex[:6]}").resolve()
    batch_dir.mkdir(parents=True, exist_ok=False)
    modes = ["single", "team"] if args.mode == "both" else [args.mode]
    results: list[dict[str, Any]] = []
    for repeat in range(1, args.repeats + 1):
        order = modes if repeat % 2 else list(reversed(modes))
        for mode in order:
            run_dir = batch_dir / f"repeat-{repeat:02d}-{mode}"
            run_dir.mkdir()
            print(f"[{repeat}/{args.repeats}] {mode}: running...", flush=True)
            command = [sys.executable, "-m", "evals.team_speed", "--worker",
                       "--mode", mode, "--provider", args.provider, "--run-dir", str(run_dir),
                       "--timeout", str(args.timeout)]
            if args.model:
                command += ["--model", args.model]
            started = time.perf_counter()
            try:
                process = subprocess.run(command, cwd=str(ROOT), capture_output=True,
                                         text=True, errors="replace", timeout=args.timeout + 30)
            except subprocess.TimeoutExpired:
                record = {"mode": mode, "repeat": repeat, "status": "hard_timeout",
                          "success": False, "elapsed_seconds": round(time.perf_counter() - started, 3)}
            else:
                path = run_dir / "result.json"
                if path.is_file():
                    record = json.loads(path.read_text(encoding="utf-8"))
                    record["repeat"] = repeat
                else:
                    record = {"mode": mode, "repeat": repeat, "status": "worker_crashed",
                              "success": False, "elapsed_seconds": round(time.perf_counter() - started, 3),
                              "stderr": process.stderr[-1000:]}
            results.append(record)
            print(f"[{repeat}/{args.repeats}] {mode}: {record['status']} "
                  f"in {record['elapsed_seconds']:.2f}s, model calls={record.get('model_calls', '?')}",
                  flush=True)

    pairs = []
    for repeat in range(1, args.repeats + 1):
        single = next((r for r in results if r["repeat"] == repeat and r["mode"] == "single"), None)
        team = next((r for r in results if r["repeat"] == repeat and r["mode"] == "team"), None)
        if single and team and single["success"] and team["success"] and team.get("team_used"):
            pairs.append({"repeat": repeat, "single_seconds": single["elapsed_seconds"],
                          "team_seconds": team["elapsed_seconds"],
                          "team_minus_single_seconds": round(team["elapsed_seconds"] - single["elapsed_seconds"], 3)})
    aggregate = {}
    for mode in modes:
        arm = [r for r in results if r["mode"] == mode]
        successful = [r for r in arm if r["success"]]
        aggregate[mode] = {
            "successes": len(successful), "trials": len(arm),
            "median_success_seconds": median(r["elapsed_seconds"] for r in successful)
            if successful else None,
            "median_success_model_calls": median(r["model_calls"] for r in successful)
            if successful else None,
        }
    summary = {"case": "dual_modules", "provider": args.provider,
               "model": args.model or settings.model, "repeats": args.repeats,
               "timing": "wall clock through independent acceptance and teammate shutdown",
               "note": "One pair is a pilot, not a statistically reliable speedup claim. Mock timings are plumbing only.",
               "results": results, "aggregate": aggregate, "comparable_pairs": pairs}
    summary_path = batch_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Summary: {summary_path}", flush=True)
    return 0 if all(r["success"] for r in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("mock", "deepseek", "anthropic"), default="mock")
    parser.add_argument("--model")
    parser.add_argument("--mode", choices=("both", "single", "team"), default="both")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=240.0,
                        help="absolute per-trial deadline in seconds")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1 or args.timeout <= 0:
        parser.error("repeats and timeout must be positive")
    if args.worker:
        if args.run_dir is None or args.mode == "both":
            parser.error("worker requires --run-dir and a single mode")
        run_worker(args.mode, args.run_dir, args.provider, args.model, args.timeout)
        return 0
    return run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
