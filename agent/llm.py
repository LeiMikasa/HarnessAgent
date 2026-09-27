"""Model access.

The loop must not care which model it is driving, and the test suite must not
need an API key.  Both goals are met by one narrow interface:

    LLMClient.create(system=..., messages=..., tools=..., max_tokens=...) -> LLMResponse

Three implementations satisfy it:

    AnthropicLLM   talks to Anthropic or any Anthropic-compatible endpoint
    MockLLM        replays a scripted conversation, fully offline
    build_client() picks one from Settings

Content blocks are normalised to plain dicts on the way in, so the rest of the
harness never touches provider SDK objects:

    {"type": "text",     "text": "..."}
    {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
    {"type": "thinking", "thinking": "...", "signature": "..."}
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from .config import Settings

# --------------------------------------------------------------------------
# Response shape
# --------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """A provider-independent assistant turn."""

    content: list[dict] = field(default_factory=list)
    stop_reason: str | None = None

    def text(self) -> str:
        return extract_text(self.content)


# --------------------------------------------------------------------------
# Block helpers -- accept dicts or raw SDK objects
# --------------------------------------------------------------------------

# 归一块化 把"可能是字典，也可能是 SDK 对象"的东西，统一变成字典
def normalize_block(raw: Any) -> dict:
    """Convert one content block into a plain dict."""
    if isinstance(raw, dict):
        return dict(raw)

    # Prefer the SDK's own serializer: it preserves thinking signatures and
    # other provider-specific fields we must echo back verbatim.
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        try:
            data = dump(exclude_none=True)
            if isinstance(data, dict):
                return data
        except TypeError:
            try:
                data = dump()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        except Exception:
            pass

    kind = getattr(raw, "type", None)
    if kind == "text":
        return {"type": "text", "text": str(getattr(raw, "text", ""))}
    if kind == "tool_use":
        return {
            "type": "tool_use",
            "id": str(getattr(raw, "id", "")),
            "name": str(getattr(raw, "name", "")),
            "input": dict(getattr(raw, "input", {}) or {}),
        }
    if kind == "thinking":
        return {
            "type": "thinking",
            "thinking": str(getattr(raw, "thinking", "")),
            "signature": str(getattr(raw, "signature", "")),
        }
    return {"type": "text", "text": str(raw)}

# 规范响应
def normalize_response(raw: Any) -> LLMResponse:
    """Convert a provider response into an `LLMResponse`."""
    content = getattr(raw, "content", raw)
    blocks: list[dict] = []
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    else:
        for item in content or []:
            item = normalize_block(item)
            # Drop reasoning blocks with no payload; some compatible
            # providers emit empty placeholders.
            if item.get("type") == "thinking" and not item.get("thinking"):
                continue
            blocks.append(item)
    return LLMResponse(content=blocks, stop_reason=getattr(raw, "stop_reason", None))


def block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def block_input(block: Any) -> dict:
    if isinstance(block, dict):
        value = block.get("input")
    else:
        value = getattr(block, "input", None)
    return dict(value) if isinstance(value, dict) else {}


def block_name(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("name", ""))
    return str(getattr(block, "name", ""))


def block_id(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("id", ""))
    return str(getattr(block, "id", ""))


def tool_use_blocks(content: Iterable[Any]) -> list[dict]:
    """Every `tool_use` block in a content list, as dicts."""
    out: list[dict] = []
    for block in content or []:
        if block_type(block) == "tool_use":
            out.append(block if isinstance(block, dict) else normalize_block(block))
    return out


def has_tool_use(content: Iterable[Any]) -> bool:
    return any(block_type(b) == "tool_use" for b in (content or []))


def extract_text(content: Any) -> str:
    """Concatenate the text of every text block."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if block_type(block) == "text":
            text = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
            if text:
                parts.append(str(text))
    return "\n".join(parts)


def make_text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def make_tool_result(tool_use_id: str, content: str, *, is_error: bool = False) -> dict:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------

# 接口协议
class LLMClient(Protocol):
    provider: str
    model: str

    def create(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 8000,
    ) -> LLMResponse:  # pragma: no cover - protocol
        ...

# 调用大模型 API 失败”这种错误
class LLMError(RuntimeError):
    """Raised when a provider call fails after retries."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# --------------------------------------------------------------------------
# Real provider
# --------------------------------------------------------------------------


class AnthropicLLM:
    """Anthropic (or Anthropic-compatible) chat client with light retry."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        timeout: float = 600.0,
    ):
        self.provider = "anthropic-compatible"
        self.model = model
        self.max_retries = max(1, max_retries)

        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - environment problem
            raise LLMError(
                "The 'anthropic' package is required for a live provider. "
                "Install it with: pip install -r requirements.txt"
            ) from exc

        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if api_key:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url
        # Let the SDK own retrying: it inspects real HTTP status codes and
        # Retry-After headers, which is far more reliable than guessing from
        # error text.  `create` below therefore makes a single attempt.
        client_kwargs["max_retries"] = max(0, max_retries)
        self._client = Anthropic(**client_kwargs)
        self.base_url = base_url

    def create(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 8000,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools

        try:
            raw = self._client.messages.create(**payload)
        except Exception as exc:  # noqa: BLE001 - normalise every provider error
            raise LLMError(
                f"{type(exc).__name__}: {exc}",
                retryable=_is_retryable(exc),
            ) from exc
        return normalize_response(raw)

# _可重试标记
_RETRYABLE_MARKERS = (
    "overloaded",
    "rate limit",
    "rate_limit",
    "429",
    "500",
    "502",
    "503",
    "504",
    "timeout",
    "timed out",
    "connection",
    "temporarily",
)


def _is_retryable(exc: Exception | None) -> bool:
    """Best-effort classification, used only to annotate `LLMError`.

    Retrying is the SDK's job (it sees status codes); this exists so callers
    can tell a transient failure from a permanent one.
    """
    if exc is None:
        return False
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


# --------------------------------------------------------------------------
# Offline model
# --------------------------------------------------------------------------


def _turn_to_blocks(step: Any, counter: list[int]) -> list[dict]:
    """Turn one scripted step into content blocks."""
    blocks: list[dict] = []

    def add_text(text: str) -> None:
        if text:
            blocks.append(make_text_block(str(text)))

    def add_tool(tool: str, tool_input: dict | None) -> None:
        counter[0] += 1
        blocks.append(
            {
                "type": "tool_use",
                "id": f"toolu_mock_{counter[0]:04d}",
                "name": str(tool),
                "input": dict(tool_input or {}),
            }
        )

    if step is None:
        add_text("(mock: script exhausted)")
        return blocks

    if isinstance(step, str):
        add_text(step)
        return blocks

    if isinstance(step, dict):
        # {"text": "...", "tool": "bash", "input": {...}} or
        # {"tool_calls": [{"tool": ..., "input": {...}}, ...]}
        if "tool_calls" in step:
            add_text(step.get("text", ""))
            for call in step["tool_calls"] or []:
                add_tool(call.get("tool") or call.get("name"), call.get("input"))
            return blocks
        if "tool" in step or "name" in step:
            add_text(step.get("text", ""))
            add_tool(step.get("tool") or step.get("name"), step.get("input"))
            return blocks
        add_text(step.get("text", ""))
        return blocks

    if isinstance(step, (list, tuple)):
        for call in step:
            if isinstance(call, str):
                add_text(call)
            elif isinstance(call, dict):
                if "text" in call and "tool" not in call and "name" not in call:
                    add_text(call["text"])
                else:
                    add_text(call.get("text", ""))
                    add_tool(call.get("tool") or call.get("name"), call.get("input"))
        return blocks

    add_text(str(step))
    return blocks


class MockLLM:
    """A deterministic, offline stand-in for a tool-calling model.

    Two modes:

      script    a list of turns consumed in order.  Each turn is a string
                (plain text answer), a dict (one tool call, optionally with
                text), or a list of such dicts (parallel tool calls).
      responder a callable ``(call_index, messages, tools) -> turn`` consulted
                before the script, for tests that must react to what the
                harness actually sent.

    The client also records every request in ``self.calls`` so tests can assert
    on the exact message history the loop produced.
    """

    def __init__(
        self,
        script: list[Any] | None = None,
        responder: Callable[[int, list[dict], list[dict]], Any] | None = None,
        *,
        model: str = "mock-1",
        default_text: str = "Done.",
    ):
        self.provider = "mock"
        self.model = model
        self.script = list(script or [])
        self.responder = responder
        self.default_text = default_text
        self.calls: list[dict] = []
        self._counter = [0]
        self._cursor = 0

    # -- introspection used by tests ---------------------------------------

    @property
    def tool_calls(self) -> list[dict]:
        """Every tool call the mock has emitted so far."""
        out = []
        for call in self.calls:
            for block in call.get("response", []):
                if block.get("type") == "tool_use":
                    out.append(block)
        return out

    def sent_tools(self) -> list[str]:
        if not self.calls:
            return []
        return [t.get("name", "") for t in self.calls[-1].get("tools", [])]

    # -- interface ----------------------------------------------------------

    def create(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 8000,
    ) -> LLMResponse:
        index = len(self.calls)
        step: Any = None

        if self.responder is not None:
            step = self.responder(index, messages, tools)
        elif self._cursor < len(self.script):
            step = self.script[self._cursor]
            self._cursor += 1
        else:
            step = self.default_text

        blocks = _turn_to_blocks(step, self._counter)
        self.calls.append(
            {
                "system": system,
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "max_tokens": max_tokens,
                "response": copy.deepcopy(blocks),
            }
        )
        return LLMResponse(content=blocks, stop_reason="end_turn" if not has_tool_use(blocks) else "tool_use")


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def build_client(
    settings: Settings,
    *,
    script: list[Any] | None = None,
    responder: Callable[[int, list[dict], list[dict]], Any] | None = None,
) -> LLMClient:
    """Pick a client from the resolved settings."""
    if settings.provider == "mock":
        return MockLLM(script, responder)
    return AnthropicLLM(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
    )
