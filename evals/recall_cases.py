"""Deterministic inputs for 50 paired context-recall cases.

Five synthetic scenario families x five placements x two history sizes.
These are 50 conditions, not 50 unrelated real-world coding tasks.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evals.context_compaction import diagnostic_output

DATASET = Path(__file__).parent / "datasets" / "recall50.json"
FAMILIES = ("facts", "constraints", "revisions", "distractors", "summary")
PLACEMENTS = ("start", "early", "middle", "late", "end")


def build_specs() -> list[dict[str, Any]]:
    return [
        {"id": f"{family}-{placement}-{pressure}", "family": family,
         "placement": placement, "pressure": pressure,
         "history_pairs": 20 if pressure == "medium" else 36,
         "result_chars": 2200 if pressure == "medium" else 4000,
         "ordinary_chars": 60000 if pressure == "medium" else 120000}
        for family in FAMILIES for placement in PLACEMENTS
        for pressure in ("medium", "high")
    ]


def load_specs(path: Path = DATASET) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or data.get("cases") != build_specs():
        raise ValueError("Dataset must match the reviewed recall50 version-1 cases")
    return data["cases"]


def placement_index(placement: str, count: int) -> int:
    fraction = {"start": 0, "early": 0.2, "middle": 0.5, "late": 0.8, "end": 1}[placement]
    return round(fraction * (count - 1))


def note(project: str, key: str, value: Any, revision: int = 1, approved: bool = True) -> str:
    return "PROJECT_NOTE " + json.dumps(
        {"project": project, "key": key, "value": value,
         "revision": revision, "approved": approved}, ensure_ascii=False,
    ) + "\n"


def materialize(spec: dict[str, Any]) -> dict[str, Any]:
    """Golden values stay in the evaluator; only history/question go to the agent."""
    digest = hashlib.sha256(spec["id"].encode()).hexdigest()
    project = "atlas-" + digest[:6]
    if spec["family"] == "constraints":
        expected = {
            "output_path": f"reports/{digest[:6]}-结果.json",
            "allowed_formats": ["json", "csv"], "network_allowed": False,
            "timeout_seconds": 30 + int(digest[:2], 16),
            "retry_budget": 1 + int(digest[2:4], 16) % 4,
            "run_mode": "dry-run-" + digest[4:8],
        }
    else:
        expected = {
            "release_code": digest[:10].upper(),
            "api_port": 8000 + int(digest[10:14], 16) % 1000,
            "deploy_region": "region-" + digest[14:18],
            "artifact_sha": digest[18:34], "service_name": "service-" + digest[34:40],
            "owner": "team-" + digest[40:46],
        }
    absent_keys = ["unassigned_contact"]
    prompt = (
        f"Recover the latest APPROVED project values for project {project!r} from earlier "
        "conversation/tool outputs or their archived transcripts. Project notes have "
        "project, key, value, revision and approved fields. For each key choose the "
        "highest revision with approved=true for this exact project; ignore other "
        "projects and unapproved proposals. Preserve JSON types and list order. "
        "If there is no approved value for a key, write null; never guess.\n"
        "Write ONLY a JSON object to answer.json containing exactly these keys: "
        + json.dumps([*expected, *absent_keys])
        + ". The expected values are not repeated here. Use file tools if needed to "
        "inspect archived context. Do not edit any other file. Finish with a concise answer."
    )
    count = spec["history_pairs"]
    anchor = placement_index(spec["placement"], count)
    fragments: dict[int, list[str]] = {i: [] for i in range(count)}
    for offset, (key, value) in enumerate(expected.items()):
        # The group anchor varies; individual fields also span different shards.
        index = (anchor + offset * 3) % count
        if spec["family"] == "revisions":
            fragments[index].append(note(project, key, f"obsolete-{digest[offset:offset + 6]}", 1))
            fragments[(index + count // 2) % count].append(note(project, key, value, 2))
            fragments[(index + count // 2 + 1) % count].append(note(project, key, "unapproved-proposal", 3, False))
        else:
            fragments[index].append(note(project, key, value))
        if spec["family"] == "distractors":
            fragments[index].insert(0, note(project + "-staging", key, "other-project-value", 99))
            fragments[(index + 1) % count].append(note(project, key, "unapproved-proposal", 99, False))
    if spec["family"] == "summary":
        noise = diagnostic_output(900, spec["ordinary_chars"])
        notes = "".join(note(project, key, value) for key, value in expected.items())
        position = placement_index(spec["placement"], len(noise) + 1)
        history = [
            {"role": "user", "content": "Project audit notes:\n" + noise[:position] + notes + noise[position:]},
            {"role": "assistant", "content": [{"type": "text", "text": "Recorded for later reference."}]},
        ]
    else:
        history = []
        for i in range(count):
            tool_id = f"history_{i:03d}"
            # Notes appear after variable amounts of noise, not always in previews.
            noise = diagnostic_output(i, spec["result_chars"])
            offset = (i % 3) * len(noise) // 3
            content = noise[:offset] + "\n" + "".join(fragments[i]) + noise[offset:]
            history.extend([
                {"role": "user", "content": f"Review prior project notes shard {i}."},
                {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id,
                 "name": "read_file", "input": {"path": f"diagnostics/shard_{i:03d}.log"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}]},
                {"role": "assistant", "content": [{"type": "text", "text": "Reviewed."}]},
            ])
    return {"spec": spec, "project": project, "prompt": prompt, "history": history,
            "expected": expected, "absent_keys": absent_keys}


def strict_equal(actual: Any, expected: Any) -> bool:
    return json.dumps(actual, ensure_ascii=False, sort_keys=True) == json.dumps(expected, ensure_ascii=False, sort_keys=True)


def grade_answer(workspace: Path, case: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    error = ""
    actual: dict[str, Any] = {}

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        path = workspace / "answer.json"
        path.resolve().relative_to(workspace.resolve())
        if path.stat().st_size > 256000:
            raise ValueError("answer.json exceeds 256 KB")
        parsed = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                            parse_constant=invalid_constant)
        if not isinstance(parsed, dict):
            raise ValueError("answer.json must be a JSON object")
        actual = parsed
    except (OSError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    correct = {key: not error and key in actual and strict_equal(actual[key], value)
               for key, value in expected.items()}
    abstained = {key: not error and key in actual and actual[key] is None for key in case["absent_keys"]}
    extras = sorted(set(actual) - set(expected) - set(case["absent_keys"]))
    return {"expected_items": len(expected), "correct_items": sum(correct.values()),
            "per_key_correct": correct, "abstention_targets": len(abstained),
            "correct_abstentions": sum(abstained.values()), "extra_keys": extras,
            "format_error": error, "actual": actual,
            "passed": not error and all(correct.values()) and all(abstained.values()) and not extras}
