"""Measure whether diagnostic verification feedback improves repair outcomes.

Both arms receive the same task, initial tools, model, and maximum number of
repair attempts. After a failed independent check, one arm gets only a generic
failure notice; the other also gets the exact failing case. The trusted
verifier remains outside the writable agent workspace.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from agent.config import load_settings
from agent.llm import LLMResponse, build_client
from agent.permissions import DENY
from agent.runtime import Runtime
from evals.team_speed import CountedLLM

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "csv_line"
VERIFIER = Path(__file__).resolve().parent / "verify_csv_line.py"
DEFAULT_OUTPUT = ROOT / ".scratch" / "evals"

TASK_PROMPT = """Implement parse_csv_line(line: str) -> list[str] in csv_line.py.
Parse exactly one CSV-like record with these rules:
- Commas separate fields. Empty input means one empty field. Leading, trailing,
  and consecutive commas create empty fields. Preserve all whitespace.
- A field may start with a double quote. Within a quoted field, commas and
  line breaks are ordinary characters; two consecutive double quotes mean
  one literal double quote.
- A quote may only start at the beginning of a field. After a quoted field's
  closing quote, only a comma or end of input is valid.
- Raise ValueError for an unclosed quoted field, a quote inside an unquoted
  field, or other text after a closing quote.
- Do not import the csv module. Do not edit any tests or other files.

The evaluator checks your result independently after you finish. Implement
the function, then give a concise final answer.
"""

GENERIC_REPAIR = (
    "Independent acceptance checks failed. Review your implementation against "
    "the original task contract and repair csv_line.py. Do not edit other files."
)
DIAGNOSTIC_REPAIR = GENERIC_REPAIR + "\n\nVerifier details:\n{detail}"

MOCK_INITIAL = '''"""First attempt intentionally misses quoted fields."""
def parse_csv_line(line: str) -> list[str]:
    return line.split(",")
'''

MOCK_REPAIRED = '''"""One-record CSV-like parser."""
def parse_csv_line(line: str) -> list[str]:
    fields = []
    field = []
    mode = "start"
    for char in line:
        if mode == "start":
            if char == ",":
                fields.append("")
            elif char == '"':
                mode = "quoted"
            else:
                field.append(char)
                mode = "plain"
        elif mode == "plain":
            if char == ",":
                fields.append("".join(field))
                field = []
                mode = "start"
            elif char == '"':
                raise ValueError("quote in unquoted field")
            else:
                field.append(char)
        elif mode == "quoted":
            if char == '"':
                mode = "after_quote"
            else:
                field.append(char)
        elif mode == "after_quote":
            if char == '"':
                field.append('"')
                mode = "quoted"
            elif char == ",":
                fields.append("".join(field))
                field = []
                mode = "start"
            else:
                raise ValueError("text after closing quote")
    if mode == "quoted":
        raise ValueError("unclosed quoted field")
    fields.append("".join(field))
    return fields
'''

BUG_CASES = ("trailing_empty", "escaped_quote", "invalid_quote")
EXPECTED_FAILURE = {
    "trailing_empty": "trailing empty field",
    "escaped_quote": "escaped quote",
    "invalid_quote": "quote inside unquoted field",
}


def seeded_source(bug_case: str) -> str:
    """One controlled fault per case, derived from the known-good parser."""
    mutations = {
        "trailing_empty": (
            '    fields.append("".join(field))\n    return fields\n',
            '    if not (line and line[-1] == ","):\n'
            '        fields.append("".join(field))\n    return fields\n',
        ),
        "escaped_quote": (
            "                field.append('\"')\n                mode = \"quoted\"\n",
            "                field.append('\"')\n                mode = \"after_quote\"\n",
        ),
        "invalid_quote": (
            '                raise ValueError("quote in unquoted field")\n',
            '                field.append(char)\n',
        ),
    }
    if bug_case not in mutations:
        raise ValueError(f"Unknown seeded bug case: {bug_case}")
    original, faulty = mutations[bug_case]
    if MOCK_REPAIRED.count(original) != 1:
        raise RuntimeError(f"Seed mutation did not match exactly once: {bug_case}")
    return MOCK_REPAIRED.replace(original, faulty, 1)


class FeedbackMockLLM:
    """For plumbing tests only: fails first, repairs only with case details."""

    provider = "mock"
    model = "feedback-mock-1"

    def __init__(self, *, seeded: bool = False) -> None:
        self.initial_written = seeded
        self.repaired = False
        self.counter = 0

    def create(self, **kwargs: Any) -> LLMResponse:
        history = str(kwargs["messages"])
        content: str | None = None
        if not self.initial_written:
            self.initial_written = True
            content = MOCK_INITIAL
        elif "Verifier details:" in history and not self.repaired:
            self.repaired = True
            content = MOCK_REPAIRED
        if content is None:
            return LLMResponse(content=[{"type": "text", "text": "Done."}], stop_reason="end_turn")
        self.counter += 1
        return LLMResponse(
            content=[{"type": "tool_use", "id": f"feedback_mock_{self.counter}",
                      "name": "write_file", "input": {"path": "csv_line.py", "content": content}}],
            stop_reason="tool_use",
        )


def verify(workspace: Path) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(VERIFIER), str(workspace)],
            cwd=str(workspace), capture_output=True, text=True,
            errors="replace", timeout=20,
        )
    except subprocess.TimeoutExpired:
        return False, "FAIL: verifier timed out after 20 seconds"
    detail = (completed.stdout + completed.stderr).strip()[:2000]
    return completed.returncode == 0, detail


def run_worker(variant: str, run_dir: Path, provider: str, model: str | None,
               max_repairs: int, timeout: float, *,
               scenario: str = "from-scratch", bug_case: str | None = None) -> dict[str, Any]:
    workspace = run_dir / "workspace"
    shutil.copytree(FIXTURE, workspace)
    if scenario == "seeded-repair":
        if bug_case not in BUG_CASES:
            raise ValueError(f"Invalid seeded bug case: {bug_case}")
        (workspace / "csv_line.py").write_text(seeded_source(bug_case), encoding="utf-8")
    elif scenario != "from-scratch":
        raise ValueError(f"Invalid scenario: {scenario}")
    settings = load_settings(
        provider=provider, model=model or ("mock-1" if provider == "mock" else None),
        workdir=workspace, state_dir=run_dir / "state", skill_dirs=[],
        max_turns=16, max_tokens=4000,
    )
    if provider != "mock" and not settings.api_key:
        raise RuntimeError("API key is missing; configure .env before live evaluation")
    llm = CountedLLM(
        FeedbackMockLLM(seeded=scenario == "seeded-repair")
        if provider == "mock" else build_client(settings)
    )
    started = time.perf_counter()
    deadline = started + timeout
    result: dict[str, Any] = {
        "case": bug_case or "csv_line", "scenario": scenario,
        "variant": variant, "provider": provider,
        "model": settings.model, "workspace": str(workspace),
        "max_repairs": max_repairs, "status": "running", "success": False,
        "first_pass": False, "attempts": [], "tool_policy": "workspace file tools only",
    }
    runtime: Runtime | None = None
    try:
        if scenario == "seeded-repair":
            seed_passed, seed_detail = verify(workspace)
            result["seed_verifier"] = seed_detail
            if seed_passed or EXPECTED_FAILURE[bug_case] not in seed_detail:
                raise RuntimeError(f"Seeded case has wrong failure: {seed_detail}")
        runtime = Runtime(
            settings, llm, teams_enabled=False, memory_enabled=False,
            compaction_enabled=False, approval=DENY,
        )
        allowed = {"read_file", "write_file", "edit_file", "glob", "grep"}
        for name in runtime.tools.names():
            if name not in allowed:
                runtime.tools.unregister(name)

        for attempt in range(max_repairs + 1):
            if time.perf_counter() >= deadline:
                result["status"] = "timeout"
                break
            if attempt == 0:
                if scenario == "seeded-repair":
                    detail = result["seed_verifier"]
                    feedback = (
                        GENERIC_REPAIR if variant == "generic" else
                        DIAGNOSTIC_REPAIR.format(detail=detail)
                    )
                    runtime.submit(TASK_PROMPT + "\n\n" + feedback)
                else:
                    runtime.submit(TASK_PROMPT)
            else:
                previous_detail = result["attempts"][-1]["verifier"]
                feedback = (
                    GENERIC_REPAIR if variant == "generic" else
                    DIAGNOSTIC_REPAIR.format(detail=previous_detail)
                )
                runtime.submit(feedback)
            if time.perf_counter() >= deadline:
                result["status"] = "timeout"
                break
            passed, detail = verify(workspace)
            if time.perf_counter() >= deadline:
                result["status"] = "timeout"
                break
            result["attempts"].append({
                "number": attempt + 1, "passed": passed, "verifier": detail,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "model_calls": llm.calls, "tool_calls": llm.tool_calls,
            })
            if attempt == 0:
                result["first_pass"] = passed
            if passed:
                result["status"] = "success"
                break
        if result["status"] == "running":
            result["status"] = "failed_acceptance"
    except Exception as exc:  # noqa: BLE001 - one failed trial must still be recorded
        result["status"] = "runtime_error"
        result["error"] = f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        if runtime is not None:
            runtime.close()
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        result["model_calls"] = llm.calls
        result["model_seconds_sum"] = round(llm.model_seconds, 3)
        result["tool_calls"] = llm.tool_calls
        result["tool_names"] = dict(llm.tool_names)
        result["success"] = result["status"] == "success"
        result["repair_rounds_used"] = max(0, len(result["attempts"]) - 1)
        result["unverified_stops"] = sum(not a["passed"] for a in result["attempts"])
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
        f"feedback-{args.scenario}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
        f"{args.provider}-{uuid.uuid4().hex[:6]}"
    )).resolve()
    batch.mkdir(parents=True, exist_ok=False)
    variants = ["generic", "diagnostic"] if args.variant == "both" else [args.variant]
    cases: list[str | None] = (
        list(BUG_CASES) if args.bug_case == "all" else [args.bug_case]
    ) if args.scenario == "seeded-repair" else [None]
    results: list[dict[str, Any]] = []
    for repeat in range(1, args.repeats + 1):
        for case_index, bug_case in enumerate(cases):
            case_name = bug_case or "csv_line"
            order = variants if (repeat + case_index) % 2 else list(reversed(variants))
            for variant in order:
                run_dir = batch / f"repeat-{repeat:02d}-{case_name}-{variant}"
                run_dir.mkdir()
                print(f"[{repeat}/{args.repeats}] {case_name}/{variant}: running...", flush=True)
                command = [
                    sys.executable, "-m", "evals.verification_feedback", "--worker",
                    "--provider", args.provider, "--variant", variant,
                    "--scenario", args.scenario,
                    "--max-repairs", str(args.max_repairs), "--timeout", str(args.timeout),
                    "--run-dir", str(run_dir),
                ]
                if bug_case:
                    command += ["--bug-case", bug_case]
                if args.model:
                    command += ["--model", args.model]
                started = time.perf_counter()
                try:
                    process = subprocess.run(
                        command, cwd=str(ROOT), capture_output=True, text=True,
                        errors="replace", timeout=args.timeout + 30,
                    )
                except subprocess.TimeoutExpired:
                    record = {"variant": variant, "case": case_name,
                              "scenario": args.scenario, "repeat": repeat,
                              "status": "hard_timeout", "success": False,
                              "elapsed_seconds": round(time.perf_counter() - started, 3)}
                else:
                    path = run_dir / "result.json"
                    if path.is_file():
                        record = json.loads(path.read_text(encoding="utf-8"))
                        record["repeat"] = repeat
                    else:
                        record = {"variant": variant, "case": case_name,
                                  "scenario": args.scenario, "repeat": repeat,
                                  "status": "worker_crashed", "success": False,
                                  "elapsed_seconds": round(time.perf_counter() - started, 3),
                                  "stderr": process.stderr[-1000:]}
                results.append(record)
                print(
                    f"[{repeat}/{args.repeats}] {case_name}/{variant}: {record['status']} "
                    f"in {record['elapsed_seconds']:.2f}s, "
                    f"attempts={len(record.get('attempts', []))}, "
                    f"model calls={record.get('model_calls', '?')}", flush=True,
                )

    aggregate: dict[str, Any] = {}
    for variant in variants:
        arm = [r for r in results if r["variant"] == variant]
        successful = [r for r in arm if r["success"]]
        aggregate[variant] = {
            "trials": len(arm), "first_passes": sum(r.get("first_pass", False) for r in arm),
            "final_passes": len(successful),
            "median_elapsed_seconds": median(r["elapsed_seconds"] for r in arm),
            "median_model_calls": median(r["model_calls"] for r in arm if "model_calls" in r)
            if all("model_calls" in r for r in arm) else None,
            "unverified_stops": sum(r.get("unverified_stops", 0) for r in arm),
        }
    pairs = []
    for repeat in range(1, args.repeats + 1):
        for bug_case in cases:
            case_name = bug_case or "csv_line"
            generic = next((r for r in results if r["repeat"] == repeat
                            and r["case"] == case_name and r["variant"] == "generic"), None)
            diagnostic = next((r for r in results if r["repeat"] == repeat
                               and r["case"] == case_name and r["variant"] == "diagnostic"), None)
            if generic and diagnostic:
                pairs.append({"repeat": repeat, "case": case_name,
                              "generic_first_pass": generic.get("first_pass"),
                              "diagnostic_first_pass": diagnostic.get("first_pass"),
                              "generic_final_pass": generic["success"],
                              "diagnostic_final_pass": diagnostic["success"],
                              "generic_seconds": generic["elapsed_seconds"],
                              "diagnostic_seconds": diagnostic["elapsed_seconds"]})
    summary = {
        "scenario": args.scenario, "cases": [case or "csv_line" for case in cases],
        "provider": args.provider,
        "model": args.model or settings.model,
        "max_repairs": args.max_repairs, "repeats": args.repeats,
        "comparison": "generic failure notice vs exact failing case; equal repair budget",
        "note": "Mock runs test plumbing only. One repetition per case is a pilot, not an effect estimate.",
        "aggregate": aggregate, "pairs": pairs, "results": results,
    }
    path = batch / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Summary: {path}", flush=True)
    return 1 if any(r["status"] in {"worker_crashed", "runtime_error", "hard_timeout"}
                    for r in results) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("mock", "deepseek", "anthropic"), default="mock")
    parser.add_argument("--model")
    parser.add_argument("--variant", choices=("both", "generic", "diagnostic"), default="both")
    parser.add_argument("--scenario", choices=("from-scratch", "seeded-repair"),
                        default="from-scratch")
    parser.add_argument("--bug-case", choices=("all", *BUG_CASES), default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="absolute per-trial deadline in seconds")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1 or args.max_repairs < 0 or args.timeout <= 0:
        parser.error("repeats and timeout must be positive; max-repairs must be nonnegative")
    if args.worker:
        if args.run_dir is None or args.variant == "both":
            parser.error("worker requires --run-dir and a single variant")
        run_worker(args.variant, args.run_dir, args.provider, args.model,
                   args.max_repairs, args.timeout,
                   scenario=args.scenario,
                   bug_case=args.bug_case if args.scenario == "seeded-repair" else None)
        return 0
    return run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
