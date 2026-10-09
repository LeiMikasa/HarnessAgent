"""Run 50 paired synthetic context-recall cases with independent JSON grading.

Defaults to a free mock. Each real batch has 100 Agent runs at repeats=1,
with multiple model calls per run. No Langfuse or paid judge is needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from agent.config import load_settings
from agent.llm import LLMResponse, build_client
from agent.permissions import DENY
from agent.runtime import Runtime
from evals.context_compaction import ALLOWED_TOOLS, MeasuredLLM
from evals.recall_cases import FAMILIES, grade_answer, load_specs, materialize
from evals.token_usage import TOKEN_FIELDS, aggregate_token_usage, summarize_calls, token_comparison

ROOT = Path(__file__).resolve().parent.parent


def save_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def suite_signature() -> str:
    """A resumed batch cannot silently switch compression/runtime implementations."""
    paths = sorted((ROOT / "agent").rglob("*.py")) + [
        Path(__file__), ROOT / "evals" / "recall_cases.py",
        ROOT / "evals" / "context_compaction.py", ROOT / "evals" / "token_usage.py",
    ]
    signature = hashlib.sha256()
    for path in paths:
        signature.update(path.relative_to(ROOT).as_posix().encode())
        signature.update(path.read_bytes())
    return signature.hexdigest()


class SuiteMockLLM:
    """Scripted gold output ONLY for smoke tests, never a model-quality result."""

    provider = "mock"
    model = "recall50-smoke"

    def __init__(self, case: dict[str, Any]):
        self.answer = {**case["expected"], **{key: None for key in case["absent_keys"]}}
        self.step = 0

    def create(self, **kwargs):
        if not kwargs["tools"]:
            return LLMResponse([{ "type": "text", "text": "Project notes were archived. Consult the transcript for exact values."}], "end_turn")
        visible = json.dumps(kwargs["messages"], ensure_ascii=False)
        if self.step == 0:
            self.step += 1
            match = re.search(r"Full transcript: ([^\"\n]+)|messages archived at ([^\]\"\n]+)", visible)
            if match:
                path = (match.group(1) or match.group(2)).strip().replace("\\\\", "\\")
                return LLMResponse([{ "type": "tool_use", "id": "smoke_read", "name": "read_file",
                                     "input": {"path": path, "limit": 2}}], "tool_use")
        if self.step <= 1:
            self.step = 2
            return LLMResponse([{ "type": "tool_use", "id": "smoke_write", "name": "write_file",
                                 "input": {"path": "answer.json", "content": json.dumps(self.answer, ensure_ascii=False)}}], "tool_use")
        return LLMResponse([{ "type": "text", "text": "Smoke fixture written."}], "end_turn")


class CheckpointLLM(MeasuredLLM):
    def __init__(self, inner, checkpoint: Path):
        super().__init__(inner)
        self.checkpoint = checkpoint

    def create(self, **kwargs):
        try:
            return super().create(**kwargs)
        finally:
            save_json(self.checkpoint, {
                "model_calls": self.calls, "summary_calls": self.summary_calls,
                "tool_calls": self.tool_calls, "context_errors": self.context_errors,
                "usage_by_call": self.usage_by_call, **summarize_calls(self.usage_by_call),
            })


def run_worker(spec: dict[str, Any], mode: str, run_dir: Path, provider: str,
               model: str | None, max_turns: int, max_tokens: int) -> dict[str, Any]:
    case = materialize(spec)
    workspace = run_dir / "workspace"
    workspace.mkdir()
    result = {"case_id": spec["id"], "family": spec["family"], "placement": spec["placement"],
              "pressure": spec["pressure"], "mode": mode, "provider": provider,
              "status": "runtime_error", "success": False, "expected_items": len(case["expected"]),
              "correct_items": 0, "abstention_targets": len(case["absent_keys"]),
              "correct_abstentions": 0, "initial_history_sha256": digest(case["history"]),
              "question_sha256": digest(case["prompt"]), "workspace": str(workspace)}
    save_json(run_dir / "case.json", case)  # outside the tool-accessible workspace
    started = time.perf_counter()
    runtime = None
    llm = None
    try:
        settings = load_settings(provider=provider, model=model or ("mock-1" if provider == "mock" else None),
                                 workdir=workspace, state_dir=workspace / ".agent", skill_dirs=[],
                                 max_turns=max_turns, max_tokens=max_tokens)
        result["model"] = settings.model
        if provider != "mock" and not settings.api_key:
            raise RuntimeError("API key is missing")
        llm = CheckpointLLM(SuiteMockLLM(case) if provider == "mock" else build_client(settings), run_dir / "telemetry.json")
        runtime = Runtime(settings, llm, teams_enabled=False, memory_enabled=False,
                          compaction_enabled=mode == "compact", approval=DENY)
        for name in runtime.tools.names():
            if name not in ALLOWED_TOOLS:
                runtime.tools.unregister(name)
        runtime.active_request = case["prompt"]
        runtime.messages.extend([*case["history"], {"role": "user", "content": case["prompt"]}])
        loop = runtime.run_turn()
        grading = grade_answer(workspace, case)
        result["grading"] = grading
        result["stop_reason"] = loop.stop_reason
        result["answer_finished"] = loop.ok
        result["agent_error"] = loop.error
        # Invalid/unfinished runs remain in the recall denominator and receive no credit.
        result["correct_items"] = grading["correct_items"] if loop.ok else 0
        result["correct_abstentions"] = grading["correct_abstentions"] if loop.ok else 0
        result["success"] = loop.ok and grading["passed"]
        result["status"] = "success" if result["success"] else "verification_failed" if loop.ok else "agent_error"
        result["archive_files"] = len(list(settings.transcripts_dir.glob("*.jsonl")))
        result["tool_result_files"] = len(list((settings.state_dir / "tool-results").glob("*")))
        result["compaction_triggered"] = bool(result["archive_files"] or result["tool_result_files"] or llm.summary_calls)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if runtime:
            result["tool_attempts"] = runtime.lead_ctx.extra.get("tool_attempts", [])
            runtime.close()
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        if llm:
            result.update({"model_calls": llm.calls, "summary_calls": llm.summary_calls,
                           "tool_calls": llm.tool_calls, "context_errors": llm.context_errors,
                           "usage_by_call": llm.usage_by_call, "tool_definition_sha256": llm.tool_definition_sha256,
                           **summarize_calls(llm.usage_by_call)})
        save_json(run_dir / "result.json", result)
    return result


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    expected = sum(r["expected_items"] for r in records)
    correct = sum(r["correct_items"] for r in records)
    abstention_targets = sum(r["abstention_targets"] for r in records)
    abstentions = sum(r["correct_abstentions"] for r in records)
    successes = sum(r["success"] for r in records)
    return {
        "trials": len(records), "passed_cases": successes,
        "case_pass_rate": successes / len(records) if records else None,
        "expected_items": expected, "correct_items": correct,
        "micro_recall": correct / expected if expected else None,
        "macro_recall": sum(r["correct_items"] / r["expected_items"] for r in records) / len(records) if records else None,
        "abstention_targets": abstention_targets, "correct_abstentions": abstentions,
        "abstention_accuracy": abstentions / abstention_targets if abstention_targets else None,
        "failures_by_status": dict(Counter(r["status"] for r in records if not r["success"])),
        "model_calls_reported": sum(r.get("model_calls", 0) for r in records),
        "summary_calls_reported": sum(r.get("summary_calls", 0) for r in records),
        "compaction_triggered_runs": sum(r.get("compaction_triggered", False) for r in records),
        "median_elapsed_seconds": median(r["elapsed_seconds"] for r in records) if records else None,
        **aggregate_token_usage(records),
    }


def summarize(records: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    modes = config["modes"]
    totals = {mode: aggregate([r for r in records if r["mode"] == mode]) for mode in modes}
    pairs = []
    for case_id in config["case_ids"]:
        for repeat in range(1, config["repeats"] + 1):
            arms = {r["mode"]: r for r in records if r["case_id"] == case_id and r["repeat"] == repeat}
            if not all(mode in arms for mode in ("full", "compact")):
                continue
            full, compact = arms["full"], arms["compact"]
            for key in ("initial_history_sha256", "question_sha256", "tool_definition_sha256"):
                if key in full and key in compact and full[key] != compact[key]:
                    raise RuntimeError(f"Paired {key} mismatch: {case_id}")
            pairs.append({"case_id": case_id, "repeat": repeat,
                          "both_passed": full["success"] and compact["success"],
                          "full_recall": full["correct_items"] / full["expected_items"],
                          "compact_recall": compact["correct_items"] / compact["expected_items"],
                          **token_comparison(full, compact)})
    successful = [r for r in records if any(p["both_passed"] and p["case_id"] == r["case_id"]
                 and p["repeat"] == r["repeat"] for p in pairs)]
    successful_totals = {mode: aggregate([r for r in successful if r["mode"] == mode]) for mode in modes}
    return {
        "config": config, "completed_runs": len(records),
        "planned_runs": len(config["case_ids"]) * config["repeats"] * len(modes),
        "is_model_quality_result": config["provider"] != "mock",
        "measurement_note": "Synthetic configuration-recall conditions. Failures remain in denominators. "
        "Provider usage only, including summary calls; missing counts are null. Cache fields are separate. "
        "Mock uses scripted gold output and has no real token measurements. A single batch does not establish generalization.",
        "totals": totals,
        "token_comparison_all_runs": token_comparison(totals["full"], totals["compact"]) if len(modes) == 2 else None,
        "token_comparison_both_passed": token_comparison(successful_totals["full"], successful_totals["compact"]) if len(modes) == 2 else None,
        "both_passed_pairs": sum(p["both_passed"] for p in pairs),
        "by_family": {family: {mode: aggregate([r for r in records if r["family"] == family and r["mode"] == mode])
                              for mode in modes} for family in sorted({r["family"] for r in records})},
        "by_pressure": {pressure: {mode: aggregate([r for r in records if r["pressure"] == pressure and r["mode"] == mode])
                                   for mode in modes} for pressure in sorted({r["pressure"] for r in records})},
        "by_placement": {placement: {mode: aggregate([r for r in records if r["placement"] == placement and r["mode"] == mode])
                                     for mode in modes} for placement in sorted({r["placement"] for r in records})},
        "paired_results": pairs, "results": records,
    }


def percentage(value: Any) -> str:
    return "未知" if value is None else f"{value * 100:.2f}%"


def write_report(batch: Path, summary: dict[str, Any]) -> None:
    save_json(batch / "summary.json", summary)
    lines = ["# 50 组上下文召回与 token 对照评测", "",
             f"模型：`{summary['config']['model']}`；提供商：`{summary['config']['provider']}`。",
             f"已记录 {summary['completed_runs']}/{summary['planned_runs']} 次 Agent 运行。", "",
             "Mock 仅验证流程；脚本化答案的分数不能用于简历或模型效果结论。" if not summary["is_model_quality_result"] else
             "本批为合成历史上的一次对照观察，不是通用 Agent 基准。", "",
             "准确召回率按正确恢复的信息项 / 全部目标信息项计算，保持 JSON 类型；未完成、超时和无法解析的输出按零项召回计入分母。",
             "整例通过要求六项全部正确、缺失值正确返回 null、格式正确且 Agent 正常结束。", "",
             "| 模式 | 准确召回 | 召回率 | 整例通过 | 正确拒答 | 模型调用（已记录） |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for mode, total in summary["totals"].items():
        lines.append(f"| {mode} | {total['correct_items']}/{total['expected_items']} | {percentage(total['micro_recall'])} | "
                     f"{total['passed_cases']}/{total['trials']} | {total['correct_abstentions']}/{total['abstention_targets']} | {total['model_calls_reported']} |")
    lines += ["", "## Token 用量", "", "所有字段按供应商含义分别统计，包含摘要调用；不自动推算费用或相加推断含缓存输入。",
              "缺失用量显示未知，不能把少量完整记录的总量当成整批用量。", "",
              "| 模式 | 输入 token | 输出 token | 缓存写入 token | 缓存读取 token | 用量完整 |",
              "| --- | ---: | ---: | ---: | ---: | --- |"]
    for mode, total in summary["totals"].items():
        values = [str(total["token_totals"][key]) if total["token_totals"][key] is not None else "未知" for key in TOKEN_FIELDS]
        lines.append(f"| {mode} | " + " | ".join(values) + f" | {total['token_usage_complete']} |")
    for name, label in (("token_comparison_all_runs", "全部运行"), ("token_comparison_both_passed", "双方均通过的配对")):
        comparison = summary[name]
        if comparison:
            reduction = comparison["token_reduction_percent"]["input_tokens"]
            lines += ["", f"{label}的供应商 `input_tokens` 降幅：" + ("未知。" if reduction is None else f"{reduction:.2f}%（负值表示增加）。")]
    lines += ["", "## 分场景召回", "", "| 场景 | 模式 | 正确项 / 总项 | 召回率 | 整例通过 |", "| --- | --- | ---: | ---: | ---: |"]
    for family, modes in summary["by_family"].items():
        for mode, total in modes.items():
            lines.append(f"| {family} | {mode} | {total['correct_items']}/{total['expected_items']} | {percentage(total['micro_recall'])} | {total['passed_cases']}/{total['trials']} |")
    lines += ["", f"双方均通过的完整配对：{summary['both_passed_pairs']}。", "",
              "逐项错误、真实 usage、停止原因和失败分类见 summary.json 与每例 result.json。",
              "未完成批次的指标只代表已记录样本；重复运行次数与不同用例数应分别报告。", ""]
    (batch / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run_parent(args) -> int:
    specs = load_specs()
    if args.family:
        specs = [s for s in specs if s["family"] == args.family]
    if args.ids:
        wanted = set(args.ids.split(","))
        if wanted - {s["id"] for s in specs}:
            raise ValueError("Unknown or filtered case ID")
        specs = [s for s in specs if s["id"] in wanted]
    if args.limit:
        # Small pilots sample across families instead of only the first family.
        ordered = sorted(specs, key=lambda s: (placement_order(s), FAMILIES.index(s["family"])))
        specs = ordered[:args.limit]
    settings = load_settings(provider=args.provider, model=args.model or ("mock-1" if args.provider == "mock" else None))
    config = {"dataset_version": 1, "dataset_sha256": digest(load_specs()), "provider": args.provider,
              "suite_sha256": suite_signature(),
              "model": settings.model, "base_url": settings.base_url, "repeats": args.repeats,
              "max_turns": args.max_turns, "max_tokens": args.max_tokens, "timeout": args.timeout,
              "case_ids": [s["id"] for s in specs], "modes": ["full", "compact"] if args.mode == "both" else [args.mode]}
    planned = len(specs) * args.repeats * len(config["modes"])
    print(f"Plan: {len(specs)} conditions, {planned} Agent runs. Each run may issue multiple model calls.", flush=True)
    if args.plan:
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0
    if args.provider != "mock" and not settings.api_key:
        print("Missing API key; no paid runs started.", file=sys.stderr)
        return 2
    batch = args.resume.resolve() if args.resume else (args.output / f"recall50-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{args.provider}-{uuid.uuid4().hex[:6]}").resolve()
    if args.resume:
        previous = json.loads((batch / "config.json").read_text(encoding="utf-8"))
        if previous != config:
            raise ValueError("Resume settings/case selection differ from saved config; repeat original flags")
    else:
        batch.mkdir(parents=True, exist_ok=False)
        save_json(batch / "config.json", config)
        save_json(batch / "dataset.json", {"version": 1, "cases": specs})
    records = []
    try:
        for repeat in range(1, args.repeats + 1):
            for index, spec in enumerate(specs):
                order = config["modes"] if (index + repeat) % 2 else list(reversed(config["modes"]))
                for mode in order:
                    run_dir = batch / f"repeat-{repeat:02d}" / spec["id"] / mode
                    result_path = run_dir / "result.json"
                    if result_path.is_file():
                        record = json.loads(result_path.read_text(encoding="utf-8"))
                    else:
                        if (run_dir / "workspace").exists():
                            # Preserve interrupted work rather than mixing it into a fresh trial.
                            run_dir.rename(run_dir.with_name(mode + "-interrupted-" + uuid.uuid4().hex[:6]))
                        run_dir.mkdir(parents=True, exist_ok=True)
                        save_json(run_dir / "spec.json", spec)
                        print(f"[{len(records)+1}/{planned}] {spec['id']}/{mode}: running", flush=True)
                        command = [sys.executable, "-m", "evals.recall50", "--worker", "--case-file", str(run_dir / "spec.json"),
                                   "--run-dir", str(run_dir), "--provider", args.provider, "--model", settings.model,
                                   "--mode", mode, "--max-turns", str(args.max_turns), "--max-tokens", str(args.max_tokens)]
                        started = time.perf_counter()
                        try:
                            process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, errors="replace", timeout=args.timeout)
                            failed_status = "worker_crashed"
                        except subprocess.TimeoutExpired:
                            process = None
                            failed_status = "hard_timeout"
                        if result_path.is_file() and process is not None:
                            record = json.loads(result_path.read_text(encoding="utf-8"))
                        else:
                            case = materialize(spec)
                            record = {"case_id": spec["id"], "family": spec["family"], "placement": spec["placement"],
                                      "pressure": spec["pressure"], "mode": mode, "provider": args.provider,
                                      "status": failed_status, "success": False,
                                      "expected_items": len(case["expected"]), "correct_items": 0,
                                      "abstention_targets": len(case["absent_keys"]), "correct_abstentions": 0,
                                      "elapsed_seconds": round(time.perf_counter() - started, 3),
                                      "token_usage_complete": False, "token_totals": dict.fromkeys(TOKEN_FIELDS)}
                            checkpoint = run_dir / "telemetry.json"
                            if checkpoint.exists():
                                telemetry = json.loads(checkpoint.read_text(encoding="utf-8"))
                                record["partial_telemetry"] = telemetry
                                for key in ("model_calls", "summary_calls", "tool_calls", "context_errors"):
                                    record[key] = telemetry[key]
                            if process is not None:
                                record["stderr"] = process.stderr[-500:]
                    record["repeat"] = repeat
                    save_json(result_path, record)
                    records.append(record)
                    write_report(batch, summarize(records, config))
                    print(f"[{len(records)}/{planned}] {spec['id']}/{mode}: {record['status']}; recall={record['correct_items']}/{record['expected_items']}", flush=True)
    except KeyboardInterrupt:
        print(f"Interrupted. Resume with the same flags plus --resume {batch}", file=sys.stderr)
        return 130
    print(f"Report: {batch / 'report.md'}\nSummary: {batch / 'summary.json'}", flush=True)
    return 1 if any(r["status"] != "success" for r in records) else 0


def placement_order(spec):
    return ("start", "early", "middle", "late", "end").index(spec["placement"]) * 2 + (spec["pressure"] == "high")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("mock", "deepseek", "anthropic"), default="mock")
    parser.add_argument("--model")
    parser.add_argument("--mode", choices=("both", "full", "compact"), default="both")
    parser.add_argument("--family", choices=FAMILIES)
    parser.add_argument("--ids", help="comma-separated case IDs")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--output", type=Path, default=ROOT / ".scratch" / "evals")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--plan", action="store_true", help="print run count/config without API calls")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--case-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.repeats, args.timeout, args.max_turns, args.max_tokens) <= 0 or (args.limit is not None and args.limit <= 0):
        parser.error("budgets and limit must be positive")
    if args.worker:
        if not args.case_file or not args.run_dir or args.mode == "both":
            parser.error("worker needs one mode, --case-file and --run-dir")
        spec = json.loads(args.case_file.read_text(encoding="utf-8"))
        run_worker(spec, args.mode, args.run_dir, args.provider, args.model, args.max_turns, args.max_tokens)
        return 0
    try:
        return run_parent(args)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
