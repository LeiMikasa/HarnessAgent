"""Export a path-free, reviewable JSON record from a graded evaluation batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


TRIAL_FIELDS = (
    "case", "mode", "repeat", "task_variant", "information_position",
    "status", "success", "original_status", "answer_finished",
    "compaction_triggered", "model_calls", "summary_calls", "tool_calls",
    "context_errors", "input_chars_sum", "input_chars_max", "elapsed_seconds",
)


def export(source: Path, destination: Path) -> None:
    summary = json.loads(source.read_text(encoding="utf-8"))
    trials = [
        {key: result[key] for key in TRIAL_FIELDS if key in result}
        for result in summary["results"]
    ]
    totals = {}
    for mode in ("full", "compact"):
        arm = [result for result in trials if result["mode"] == mode]
        totals[mode] = {
            "successes": sum(result["success"] for result in arm),
            "trials": len(arm),
            "input_chars_sum": sum(result["input_chars_sum"] for result in arm),
            "model_calls": sum(result["model_calls"] for result in arm),
            "summary_calls": sum(result["summary_calls"] for result in arm),
            "context_errors": sum(result["context_errors"] for result in arm),
            "compaction_triggered": sum(result["compaction_triggered"] for result in arm),
        }
    data = {
        "provider": summary["provider"],
        "model": summary["model"],
        "cases": summary["cases"],
        "repeats": summary["repeats"],
        "pairs_of_history": summary["pairs_of_history"],
        "chars_per_result": summary["chars_per_result"],
        "grading_note": summary.get("grading_note"),
        "measurement_note": summary["measurement_note"].replace(
            " Mock validates plumbing only.", ""
        ) if summary["provider"] != "mock" else summary["measurement_note"],
        "aggregate_by_case": summary["aggregate"],
        "totals": totals,
        "trials": trials,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Exported {len(trials)} runs to {destination}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    export(args.summary, args.output)


if __name__ == "__main__":
    main()
