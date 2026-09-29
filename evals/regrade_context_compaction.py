"""Regrade saved context-compaction outputs without another model call.

The initial retention grader required exact bytes. Normal text files often end
with one LF or CRLF, so that rule incorrectly rejected correct answers. This
script preserves the original summary and writes a separate corrected copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from statistics import median
from typing import Any

from evals.context_compaction import verify_code_answer
from evals.team_speed import verify as verify_dual
from evals.verification_feedback import verify as verify_csv


def regrade_result(original: dict[str, Any]) -> dict[str, Any]:
    result = dict(original)
    result["original_status"] = original["status"]
    result["original_verifier"] = original.get("verifier")
    workspace = Path(result["workspace"])
    if result["case"] == "coding":
        passed, detail = (
            verify_dual(workspace) if result.get("task_variant") == "dual_modules"
            else verify_csv(workspace)
        )
    else:
        seed = result["repeat"]
        expected = hashlib.sha256(
            f"context-retention:{seed}".encode()
        ).hexdigest()[:8].upper()
        passed = verify_code_answer(workspace / "answer.txt", expected)
        detail = (
            "exact historical value recovered (optional final newline accepted)"
            if passed else "historical value missing or incorrect"
        )
    result["verifier"] = detail
    result["status"] = (
        "success" if passed and result.get("answer_finished", False)
        else "verification_failed" if not passed else "agent_error"
    )
    result["success"] = result["status"] == "success"
    return result


def regrade(source: Path, destination: Path) -> None:
    summary = json.loads(source.read_text(encoding="utf-8"))
    results = [regrade_result(r) for r in summary["results"]]
    aggregate: dict[str, Any] = {}
    for case in summary["cases"]:
        aggregate[case] = {}
        for mode in ("full", "compact"):
            arm = [r for r in results if r["case"] == case and r["mode"] == mode]
            successful = [r for r in arm if r["success"]]
            aggregate[case][mode] = {
                "successes": len(successful),
                "trials": len(arm),
                "context_errors": sum(r.get("context_errors", 0) for r in arm),
                "median_success_seconds": (
                    median(r["elapsed_seconds"] for r in successful)
                    if successful else None
                ),
                "median_input_chars_sum": (
                    median(r["input_chars_sum"] for r in arm)
                    if all("input_chars_sum" in r for r in arm) else None
                ),
            }
    pairs = []
    for repeat in range(1, summary["repeats"] + 1):
        for case in summary["cases"]:
            arm = {r["mode"]: r for r in results
                   if r["repeat"] == repeat and r["case"] == case}
            if all(mode in arm and arm[mode]["success"] for mode in ("full", "compact")):
                full, compact = arm["full"], arm["compact"]
                pairs.append({
                    "repeat": repeat, "case": case,
                    "full_seconds": full["elapsed_seconds"],
                    "compact_seconds": compact["elapsed_seconds"],
                    "full_input_chars": full["input_chars_sum"],
                    "compact_input_chars": compact["input_chars_sum"],
                })
    corrected = dict(summary)
    corrected.update({
        "grading_note": (
            "Regraded saved artifacts without model calls. Exact release code "
            "allows one conventional final LF or CRLF; original summary is preserved."
        ),
        "source_summary": str(source.resolve()),
        "aggregate": aggregate,
        "comparable_pairs": pairs,
        "results": results,
    })
    destination.write_text(
        json.dumps(corrected, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    changed = [r for r in results if r["status"] != r["original_status"]]
    print(f"Regraded {len(results)} runs; status changed for {len(changed)}.")
    print(f"Corrected summary: {destination}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    regrade(args.summary, args.output or args.summary.with_name("summary.regraded.json"))


if __name__ == "__main__":
    main()
