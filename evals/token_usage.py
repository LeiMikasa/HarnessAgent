"""Aggregate provider-reported tokens without estimating missing measurements.

Cache counters remain separate. Do not infer inclusive input or billing totals
by adding fields whose semantics may differ between compatible providers.
"""

from __future__ import annotations

from typing import Any


TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
)


def is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def summarize_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    usages = [call["usage"] for call in calls]
    totals = {}
    for name in TOKEN_FIELDS:
        values = [usage.get(name) if isinstance(usage, dict) else None for usage in usages]
        totals[name] = sum(values) if values and all(is_count(v) for v in values) else None
    reported = sum(isinstance(usage, dict) and bool(usage) for usage in usages)
    return {
        "usage_calls": reported,
        "usage_missing_calls": len(calls) - reported,
        "token_usage_complete": all(totals[name] is not None for name in ("input_tokens", "output_tokens")),
        "token_totals": totals,
    }


def aggregate_token_usage(results: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {}
    for name in TOKEN_FIELDS:
        values = [result.get("token_totals", {}).get(name) for result in results]
        totals[name] = sum(values) if values and all(is_count(v) for v in values) else None
    aggregate = {
        "token_usage_complete": bool(results) and all(result.get("token_usage_complete", False) for result in results),
        "token_totals": totals,
    }
    for name in ("usage_calls", "usage_missing_calls"):
        values = [result.get(name) for result in results]
        aggregate[name] = sum(values) if values and all(is_count(v) for v in values) else None
    return aggregate


def token_comparison(full: dict[str, Any], compact: dict[str, Any]) -> dict[str, Any]:
    full_totals = full.get("token_totals", {})
    compact_totals = compact.get("token_totals", {})
    reductions = {}
    for name in TOKEN_FIELDS:
        before, after = full_totals.get(name), compact_totals.get(name)
        reductions[name] = (
            round((1 - after / before) * 100, 4)
            if is_count(before) and before > 0 and is_count(after) else None
        )
    return {
        "full_token_totals": {name: full_totals.get(name) for name in TOKEN_FIELDS},
        "compact_token_totals": {name: compact_totals.get(name) for name in TOKEN_FIELDS},
        "token_reduction_percent": reductions,
    }
