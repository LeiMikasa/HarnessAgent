"""Run rebuilt realistic recall cases with minicode and DeepSeek or Codex.

Default: one free mock pair. Real runs require an explicit provider. --all is an
explicit opt-in to 100 Agent runs; each run may make several Codex requests.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

from agent.context import ContextCompactor
from agent.config import load_settings
from evals.realistic_cases import (DATASET, DATASET_SEED, DATASET_VERSION, FAMILIES,
                                   grade, load_specs, materialize)
from evals.recall50 import ROOT, digest, run_worker, save_json, suite_signature, summarize, write_report


def signature():
    value = hashlib.sha256(suite_signature().encode())
    for path in (Path(__file__), DATASET, Path(__file__).with_name("realistic_cases.py"),
                 Path(__file__).with_name("codex_llm.py")):
        value.update(path.read_bytes())
    return value.hexdigest()


def select_specs(ids=None, limit=None, all_cases=False):
    specs = load_specs()
    if ids:
        wanted = set(ids.split(","))
        if wanted - {s["id"] for s in specs}:
            raise ValueError("Unknown case ID")
        specs = [s for s in specs if s["id"] in wanted]
    else:
        # Pilot samples rotate across families before moving to the next domain.
        domains = list(dict.fromkeys(s["brief"] for s in specs))
        specs.sort(key=lambda s: (domains.index(s["brief"]), FAMILIES.index(s["family"])))
        specs = specs[:limit or (len(specs) if all_cases else 1)]
    return specs


def ensure_plan_auth():
    result = subprocess.run(["codex", "login", "status"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=15)
    if result.returncode or "chatgpt" not in (result.stdout + result.stderr).lower():
        raise ValueError("ChatGPT-plan Codex login required; API-key auth is not enabled by this evaluator")


def write_real_report(batch, records, config):
    summary = summarize(records, config)
    summary["dataset_name"] = "minicode-realistic-recall50"
    if config["provider"] == "codex":
        provider_note = (
            "Fresh ephemeral Codex session per model request, built-in tools disabled and checked. "
            "minicode executes file tools and compression. JSON tool calls are an evaluation transport, "
            "not native API function calls. Token usage includes Codex CLI system/protocol overhead; "
            "cached input is a subset of input, not extra input. Missing usage remains unknown. "
            "Requested max_tokens is a prompt hint, not an enforced CLI output limit. ")
    elif config["provider"] == "deepseek":
        provider_note = (
            "DeepSeek uses the configured Anthropic-compatible API with native tool calls. "
            "Reported token usage comes from API responses, including summary calls; no Codex CLI "
            "overhead is included. max_tokens is sent as the API output limit. Missing usage remains "
            "unknown, and cached input is not added blindly to provider input tokens. ")
    else:
        provider_note = "Mock validates evaluation plumbing only, with no real model requests. "
    summary["measurement_note"] = (
        "50 synthetic work-handoff conditions, not real production samples. " + provider_note +
        "Only actually triggered compression can support compression conclusions. "
        "Both arms use the same general history-first/completion guidance and remaining-turn "
        "reminders; no grader answers are injected. Detailed local payloads are stored outside "
        "the tool workspace in each run's trace/ directory. "
        "Mock scores only validate the harness and must not be reported as model recall.")
    write_report(batch, summary)
    report = batch / "report.md"
    text = report.read_text(encoding="utf-8")
    text = text.replace("# 50 组上下文召回与 token 对照评测", "# minicode 新版工作交接召回评测")
    text += "\n\n## 首次请求与累计开销\n\n"
    text += "| 场景 | 模式 | 首次输入 token | 累计输入 token | 工具调用 |\n|---|---|---:|---:|---:|\n"
    for record in records:
        calls = record.get("usage_by_call", [])
        first_usage = calls[0].get("usage") if calls else None
        first_input = first_usage.get("input_tokens") if isinstance(first_usage, dict) else None
        total_input = record.get("token_totals", {}).get("input_tokens")
        text += (f"| {record['case_id']} | {record['mode']} | {first_input if first_input is not None else '未知'} | "
                 f"{total_input if total_input is not None else '未知'} | {record.get('tool_calls', '未知')} |\n")
    text += "\n首次请求变短不保证累计输入减少：后续归档读取、工具回显和额外模型轮次都会再次计入输入。\n"
    text += ("\n## 排查每轮行为\n\n每个场景、每种模式下的 `trace/timeline.md` 按顺序展示模型可见回复、"
             "工具参数及结果预览；`trace/model-calls/call-*/request.json` 和 `response.json` 保留完整调用内容，"
             "失败时写入 `error.json`。`trace/tools.jsonl` 保留完整工具结果，包括权限和参数校验拒绝。"
             "这些文件位于模型工作区外，不会作为新增题目资料提供给 Agent。\n")
    text += ("\n\n## 本批评测边界\n\n" + summary["measurement_note"]
             + "\n\n本批次的历史和任务相同，压缩阈值使用生产默认值，没有为通过评测降低阈值。"
             "所有逐项答案、未知信息拒答和输入文件完整性均由 Python 独立验收。\n")
    report.write_text(text, encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("mock", "codex", "deepseek"), default="mock")
    parser.add_argument("--model", help="DeepSeek: use MODEL_ID from .env unless supplied; Codex: default gpt-6.1-sol")
    parser.add_argument("--ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--all", action="store_true", help="Run all 50 cases (100 Agent runs)")
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--timeout", type=float, default=480, help="Per-run deadline checked before each model call")
    parser.add_argument("--output", type=Path, default=ROOT / ".scratch" / "evals")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--resume", type=Path)
    args = parser.parse_args()
    if min(args.max_turns, args.max_tokens, args.timeout) <= 0 or (args.limit is not None and args.limit <= 0):
        parser.error("Budgets and limit must be positive")
    if sum(bool(value) for value in (args.ids, args.limit, args.all)) > 1:
        parser.error("Use only one of --ids, --limit and --all")
    specs = select_specs(args.ids, args.limit, args.all)
    settings = load_settings(provider="deepseek", model=args.model) if args.provider == "deepseek" else None
    if settings is not None:
        model = settings.model
    elif args.provider == "codex":
        model = args.model or "gpt-6.1-sol"
    else:
        model = "recall50-smoke"
    config = {"dataset_version": DATASET_VERSION, "dataset_seed": DATASET_SEED,
              "dataset_sha256": digest(load_specs()), "suite_sha256": signature(),
              "provider": args.provider, "model": model,
              "base_url": settings.base_url if settings else None,
              "local_trace_version": 1, "completion_guidance": "history-first+turn-budget-v1",
              "case_ids": [s["id"] for s in specs], "modes": ["full", "compact"], "repeats": 1,
              "max_turns": args.max_turns, "max_tokens": args.max_tokens, "timeout": args.timeout,
              "compaction_thresholds": {name: getattr(ContextCompactor, name) for name in (
                  "CONTEXT_TOKEN_LIMIT", "TOOL_RESULT_BATCH_TOKEN_LIMIT", "LARGE_RESULT_TOKEN_LIMIT",
                  "SUMMARY_INPUT_TOKEN_LIMIT", "SNIP_MAX_MESSAGES", "SNIP_KEEP_MESSAGES",
                  "KEEP_RECENT_RESULTS", "KEEP_RECENT_MESSAGES", "PERSIST_PREVIEW_CHARS", "FIT_PREVIEW_CHARS")}}
    print(f"Plan: {len(specs)} rebuilt cases, {len(specs)*2} Agent runs; provider={args.provider}", flush=True)
    if args.plan:
        print(json.dumps({**config, "family_counts": dict(Counter(s["family"] for s in load_specs()))}, ensure_ascii=False, indent=2))
        return 0
    if args.provider == "codex":
        ensure_plan_auth()
    elif settings is not None and not settings.api_key:
        raise ValueError("DeepSeek API key is missing; no model requests started")
    batch = (args.resume or args.output / (
        f"realistic-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{args.provider}-{uuid.uuid4().hex[:6]}"
    )).resolve()
    if args.resume:
        if json.loads((batch / "config.json").read_text(encoding="utf-8")) != config:
            raise ValueError("Resume configuration or code signature changed")
    else:
        batch.mkdir(parents=True, exist_ok=False)
        save_json(batch / "config.json", config)
        save_json(batch / "dataset.json", {"version": DATASET_VERSION, "cases": specs})
    records = []
    consecutive_call_errors = 0
    stop_reason = None
    for index, spec in enumerate(specs):
        # Counterbalance order; no conversation is shared between arms or calls.
        order = config["modes"] if index % 2 == 0 else list(reversed(config["modes"]))
        for mode in order:
            run_dir = batch / spec["id"] / mode
            record = None
            if (run_dir / "result.json").exists():
                saved = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
                if saved.get("status") != "interrupted":
                    record = saved
            if record is None:
                if run_dir.exists():
                    run_dir.rename(run_dir.with_name(mode + "-interrupted-" + uuid.uuid4().hex[:6]))
                run_dir.mkdir(parents=True)
                print(f"[{len(records)+1}/{len(specs)*2}] {spec['id']}/{mode}: running", flush=True)
                record = run_worker(spec, mode, run_dir, args.provider,
                                    model,
                                    args.max_turns, args.max_tokens, case=materialize(spec),
                                    grader=grade, run_timeout=args.timeout)
            record["repeat"] = 1
            save_json(run_dir / "result.json", record)
            records.append(record)
            write_real_report(batch, records, config)
            print(f"[{len(records)}/{len(specs)*2}] {spec['id']}/{mode}: {record['status']}; "
                  f"recall={record['correct_items']}/{record['expected_items']}; "
                  f"compaction={record.get('compaction_triggered', False)}; "
                  f"tools={record.get('tool_calls', 0)}; tokens={record.get('token_totals')}", flush=True)
            error_text = str(record.get("agent_error") or record.get("error") or "").lower()
            call_failed = record["status"] == "runtime_error" or (
                record["status"] == "agent_error" and record.get("stop_reason") == "error")
            consecutive_call_errors = consecutive_call_errors + 1 if call_failed else 0
            if any(marker in error_text for marker in (
                    "usage limit", "rate limit", "rate_limit", "quota", "out of credits", "insufficient credits",
                    "insufficient balance", "payment required", "authenticationerror", "401", "402")):
                stop_reason = f"{args.provider} account authentication/balance/usage/rate limit reached; remaining trials were not run"
            elif consecutive_call_errors >= 3:
                stop_reason = "Three consecutive model/runtime call failures; remaining trials were not run"
            if stop_reason:
                save_json(batch / "stopped.json", {"reason": stop_reason, "completed_runs": len(records),
                                                  "planned_runs": len(specs) * 2})
                print(f"Stopped: {stop_reason}", flush=True)
                break
        if stop_reason:
            break
    print(f"Report: {batch / 'report.md'}\nSummary: {batch / 'summary.json'}")
    return int(any(not r["success"] for r in records))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"Evaluation error: {exc}", file=sys.stderr)
        raise SystemExit(2)
