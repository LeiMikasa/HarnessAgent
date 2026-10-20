"""Offline text-token estimates for compaction, never provider usage.

Chinese characters cost roughly 0.6 tokens and other characters 0.3. A 20%
margin is included once in every returned estimate. This JSON/text proxy is
not a model tokenizer and cannot guarantee that a provider request will fit.
"""

from __future__ import annotations

import json
import re
from typing import Any


ESTIMATOR_ID = "chars-zh0.6-other0.3-margin1.2-v1"
_CHINESE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]"
)


def _token_weight(text: str) -> int:
    """Hundredths of an estimated token, including the margin, before rounding."""
    chinese = sum(1 for _ in _CHINESE.finditer(text))
    return (len(text) + chinese) * 36


def estimate_text_tokens(text: str) -> int:
    """Return ceil((Chinese * 0.6 + other * 0.3) * 1.2)."""
    return (_token_weight(text) + 99) // 100


def estimate_tokens(value: Any) -> int:
    """Estimate structured content without escaping Chinese to ASCII."""
    return estimate_text_tokens(json.dumps(value, default=str, ensure_ascii=False))


def estimate_request_tokens(system: str, messages: list, tools: list | None = None) -> int:
    """Include system, message payloads, tool schemas and JSON framing.

    The provider's actual chat template may differ. Output tokens are separate
    from this input estimate; cache hits still occupy input context.
    """
    return estimate_tokens({"system": system, "messages": messages, "tools": tools or []})


def estimate_request_breakdown(system: str, messages: list, tools: list | None = None) -> dict[str, int]:
    """Partition the same request estimate without losing or double counting.

    A tool_result block, including its metadata/content, belongs to tool_results.
    All remaining message content and message framing (including assistant tool
    calls/thinking) belongs to conversation. Only outer request framing belongs
    to request_structure. Empty system/tools values are framing, not content.

    Distribute rounding by largest remainder so displayed parts add up exactly
    to estimate_request_tokens(), instead of rounding every part up separately.
    """
    def weight(value: Any) -> int:
        return _token_weight(json.dumps(value, default=str, ensure_ascii=False))

    tools = tools or []
    total_weight = weight({"system": system, "messages": messages, "tools": tools})
    message_weight = weight(messages)
    result_weight = sum(
        weight(block)
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    weights = {
        "system_prompt": weight(system) if system else 0,
        "tool_definitions": weight(tools) if tools else 0,
        "tool_results": result_weight,
        "conversation": message_weight - result_weight if messages else 0,
    }
    weights["request_structure"] = total_weight - sum(weights.values())
    parts = {name: value // 100 for name, value in weights.items()}
    remainder = (total_weight + 99) // 100 - sum(parts.values())
    for name in sorted(weights, key=lambda key: weights[key] % 100, reverse=True)[:remainder]:
        parts[name] += 1
    return parts
