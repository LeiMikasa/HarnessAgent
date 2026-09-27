#!/usr/bin/env python3
"""Preflight: verify the live provider before trusting the harness.

The offline test suite proves the harness's *mechanics*.  It cannot prove that
the provider path works, because that needs a real key and a real endpoint.
This script closes that gap in one command:

    python preflight.py

It reports the resolved configuration, then makes one minimal call per
candidate model id and prints the exact outcome.  Nothing here is destructive:
every request is a single short prompt with no tools.

    --models a,b,c   probe these model ids instead of the built-in candidates
    --tools          also verify a tool-calling round trip (the real risk)
    --quiet          only print the verdict lines
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Model ids worth trying, cheapest first.  DeepSeek's Anthropic-compatible
#: endpoint is the default target; the Claude names are included so the same
#: script verifies a switch to Anthropic itself.
CANDIDATE_MODELS: tuple[str, ...] = (
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "claude-sonnet-4-6",
)

PROBE_TOOL = {
    "name": "echo",
    "description": "Echo a string back. Used only to verify tool calling.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


def _mask(value: str | None) -> str:
    if not value:
        return "(missing)"
    return f"{value[:6]}...{value[-4:]} ({len(value)} chars)" if len(value) > 12 else "(short)"


def _verdict(ok: bool, label: str, detail: str = "") -> None:
    mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the live model provider.")
    parser.add_argument("--models", help="Comma-separated model ids to probe.")
    parser.add_argument("--tools", action="store_true", help="Also verify tool calling.")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--env-file", dest="env_file", default=".env")
    args = parser.parse_args(argv)

    from agent.config import load_settings

    settings = load_settings(args.env_file if Path(args.env_file).is_file() else None)

    print("=" * 72)
    print("resolved configuration")
    print("=" * 72)
    print(f"  provider    {settings.provider}")
    print(f"  base_url    {settings.base_url or '(SDK default: api.anthropic.com)'}")
    print(f"  model       {settings.model}")
    print(f"  api_key     {_mask(settings.api_key)}")
    print(f"  workdir     {settings.workdir}")
    print(f"  env file    {settings.env_file or '(none found)'}")
    print()

    if settings.provider == "mock":
        _verdict(False, "provider is 'mock'", "set AGENT_PROVIDER=deepseek in .env")
        return 2

    try:
        import anthropic  # noqa: F401
    except ImportError:
        _verdict(False, "the 'anthropic' package", "pip install -r requirements.txt")
        print("\nNothing else can be verified until that import works.")
        return 2

    if not settings.api_key:
        _verdict(False, "api_key", "set ANTHROPIC_API_KEY in .env")
        return 2

    from agent.llm import AnthropicLLM, LLMError

    if args.models:
        candidates = tuple(m.strip() for m in args.models.split(",") if m.strip())
    else:
        candidates = tuple(dict.fromkeys((settings.model, *CANDIDATE_MODELS)))

    print("=" * 72)
    print("probing model ids (one minimal request each)")
    print("=" * 72)

    working: list[str] = []
    for model in candidates:
        client = AnthropicLLM(
            model=model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_retries=1,
            timeout=30.0,
        )
        try:
            response = client.create(
                system="Reply with the single word: ok",
                messages=[{"role": "user", "content": "ping"}],
                tools=[],
                max_tokens=16,
            )
        except LLMError as exc:
            _verdict(False, model, str(exc)[:300])
            continue
        except Exception as exc:  # noqa: BLE001
            _verdict(False, model, f"{type(exc).__name__}: {str(exc)[:280]}")
            if not args.quiet:
                traceback.print_exc(limit=3)
            continue

        text = response.text().strip().replace("\n", " ")[:60]
        _verdict(True, model, f"replied {text!r}")
        working.append(model)

    print()
    if not working:
        print("No candidate model id worked. The base_url or key is the likely cause,")
        print("so check the error text above rather than the model name.")
        return 1

    print(f"Usable model ids: {', '.join(working)}")
    print(f"Set MODEL_ID={working[0]} in .env")
    print()

    # ---- tool calling, which is what the harness actually depends on -------
    if args.tools:
        model = working[0]
        print("=" * 72)
        print(f"tool-calling round trip with {model!r}")
        print("=" * 72)
        client = AnthropicLLM(
            model=model, api_key=settings.api_key, base_url=settings.base_url,
            max_retries=1, timeout=60.0,
        )
        messages = [
            {
                "role": "user",
                "content": "Call the echo tool with text='hello'. Then stop.",
            }
        ]
        try:
            first = client.create(
                system="You are a tool-using agent. Use the echo tool when asked.",
                messages=messages,
                tools=[PROBE_TOOL],
                max_tokens=256,
            )
        except Exception as exc:  # noqa: BLE001
            _verdict(False, "first model call", f"{type(exc).__name__}: {exc}")
            return 1

        from agent.llm import extract_text, tool_use_blocks

        calls = tool_use_blocks(first.content)
        _verdict(bool(calls), "model emitted a tool_use block",
                 f"{len(calls)} call(s)" if calls else extract_text(first.content)[:120])
        if not calls:
            print("\nThe endpoint responded but did not use tools. A harness needs a")
            print("tool-calling model; check that this model id supports function calling.")
            return 1

        block = calls[0]
        print(f"         tool={block.get('name')!r} input={block.get('input')!r}")
        messages.append({"role": "assistant", "content": first.content})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": block.get("id", ""),
                        "content": "hello",
                    }
                ],
            }
        )
        try:
            second = client.create(
                system="You are a tool-using agent. Use the echo tool when asked.",
                messages=messages,
                tools=[PROBE_TOOL],
                max_tokens=256,
            )
        except Exception as exc:  # noqa: BLE001
            _verdict(False, "second model call (tool_result accepted)",
                     f"{type(exc).__name__}: {exc}")
            print("\nThe endpoint rejected the tool_result turn. This is the shape the")
            print("whole harness depends on, so nothing else will work until it passes.")
            return 1

        _verdict(True, "tool_result accepted", extract_text(second.content).strip()[:80] or "(empty text)")
        print()

    print("=" * 72)
    print("Next:  python -m agent \"say hello\"        # one live turn")
    print("       python -m agent                     # interactive")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
