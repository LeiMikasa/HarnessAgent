"""Terminal entry point.

    python -m agent                     interactive session
    python -m agent "fix the failing test"
    python -m agent --mock --demo       offline self-check, no API key needed
    python -m agent --status            print the resolved configuration

Inside the REPL, lines starting with `:` are harness commands, everything else
is sent to the agent:

    :help      :status    :tools     :skills   :todos
    :tasks     :team      :mcp       :memory   :clear   :quit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .config import load_settings
from .runtime import Runtime

BANNER = (
    "\033[36mHarnessAgent\033[0m -- a coding-agent harness.\n"
    "Type a request, or :help for harness commands. "
    ":quit (or q) to exit.\n"
)

#: Accepted both bare and with a leading colon.
QUIT_WORDS = frozenset({"q", "quit", "exit", "bye"})

HELP = """\
Harness commands
  :help              this text
  :status            configuration, tool count, counters
  :tools             every registered tool
  :skills            the skill catalog
  :todos             the current plan
  :tasks             the shared task board
  :team              teammate roster
  :mcp               connected MCP servers
  :memory            stored memory records
  :notes             diagnostics from the last turn
  :clear             forget this conversation (keeps tasks and memory)
  :quit              exit  (also: q, exit, Ctrl+C, Ctrl+D)
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="A modular coding-agent harness.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("request", nargs="*", help="Run one request and exit.")
    parser.add_argument("--provider", choices=["deepseek", "anthropic", "mock"], help="Model backend.")
    parser.add_argument("--model", help="Model id override.")
    parser.add_argument("--base-url", dest="base_url", help="API base URL override.")
    parser.add_argument("--workdir", help="Workspace root (default: cwd).")
    parser.add_argument("--env-file", dest="env_file", help="Path to a .env file.")
    parser.add_argument("--mock", action="store_true", help="Use the offline scripted model.")
    parser.add_argument("--demo", action="store_true", help="Run a scripted offline demo and exit.")
    parser.add_argument("--status", action="store_true", help="Print configuration and exit.")

    parser.add_argument(
        "--approval",
        choices=["ask", "allow", "deny"],
        help="Non-interactive approval policy (default: ask).",
    )
    parser.add_argument("--allow-all", action="store_true", help="Shorthand for --approval allow.")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--verbose", "-v", action="store_true", help="Show progress and detailed event logs.")
    output.add_argument("--quiet", action="store_true", help="Show only the final answer.")

    parser.add_argument("--no-teams", action="store_true", help="Disable teammates and worktrees.")
    parser.add_argument("--no-memory", action="store_true", help="Disable the memory system.")
    parser.add_argument("--no-compaction", action="store_true", help="Disable context compaction.")
    parser.add_argument(
        "--no-plan-gate",
        action="store_true",
        help="Do not require teammates to get plan approval before editing.",
    )
    return parser


def _demo_script() -> list[Any]:
    """A scripted conversation that exercises the harness offline."""
    return [
        # 1. plan the work
        {
            "text": "I'll plan this first.",
            "tool_calls": [
                {
                    "tool": "todo_write",
                    "input": {
                        "todos": [
                            {"content": "write a greeting module", "status": "in_progress"},
                            {"content": "verify it imports", "status": "pending"},
                        ]
                    },
                }
            ],
        },
        # 2. write the file
        {
            "tool_calls": [
                {
                    "tool": "write_file",
                    "input": {"path": "greeting.py", "content": "def hello(name):\n    return f'Hello, {name}!'\n"},
                }
            ]
        },
        # 3. verify it
        {
            "tool_calls": [
                {"tool": "bash", "input": {"command": "python -c \"import greeting; print(greeting.hello('world'))\""}}
            ]
        },
        # 4. close out the plan
        {
            "tool_calls": [
                {
                    "tool": "todo_write",
                    "input": {
                        "todos": [
                            {"content": "write a greeting module", "status": "completed"},
                            {"content": "verify it imports", "status": "completed"},
                        ]
                    },
                }
            ]
        },
        # 5. finish
        "Wrote greeting.py with a hello(name) function and verified it imports and runs.",
    ]


def _print(message: str = "") -> None:
    print(message)


def _print_progress(message: str) -> None:
    """Keep the final answer on stdout and live status on stderr."""
    print(message, file=sys.stderr, flush=True)


def _repl(runtime: Runtime) -> int:
    _print(BANNER)
    while True:
        try:
            line = input("\001\033[36m\002agent >> \001\033[0m\002")
        except (EOFError, KeyboardInterrupt):
            _print()
            break

        text = line.strip()
        if not text:
            continue

        # Bare quit words are accepted too.  Without this, typing `q` or `exit`
        # would be sent to the model as a work request -- which costs money and
        # is a genuinely surprising way to try to leave.
        if text.lower() in QUIT_WORDS:
            break

        if text.startswith(":"):
            if _command(runtime, text[1:].strip()):
                break
            continue

        try:
            answer = runtime.submit(text)
        except Exception as exc:  # noqa: BLE001 - a REPL must not die
            _print(f"\033[31mError: {type(exc).__name__}: {exc}\033[0m")
            continue
        _print()
        _print(answer)
        _print()
    return 0


def _command(runtime: Runtime, raw: str) -> bool:
    """Handle a `:command`.  Returns True when the REPL should exit."""
    name, _, argument = raw.partition(" ")
    name = name.lower()

    if name in ("quit", "exit", "q"):
        return True
    if name in ("help", "h", "?"):
        _print(HELP)
    elif name == "status":
        _print(runtime.status())
    elif name == "tools":
        for tool in runtime.tools:
            _print(f"  {tool.name:24} [{tool.source}] {tool.description[:70]}")
    elif name == "skills":
        _print(runtime.skills.catalog())
    elif name == "todos":
        _print(runtime.todos.render())
    elif name == "tasks":
        _print(runtime.tasks.render_list())
    elif name == "team":
        _print(runtime.summon_report())
    elif name == "mcp":
        names = runtime.mcp.names()
        _print(", ".join(names) if names else "No MCP servers connected.")
    elif name == "memory":
        records = runtime.memory.list_records()
        if not records:
            _print("No memory records.")
        for record in records:
            _print(f"  [{record['type']}] {record['name']}: {record['description']}")
    elif name == "notes":
        _print("\n".join(runtime.notes) if runtime.notes else "No notes.")
    elif name == "clear":
        runtime.messages.clear()
        _print("Conversation cleared.")
    else:
        _print(f"Unknown command :{name}. Try :help")
    return False


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    overrides: dict[str, Any] = {}
    if args.provider:
        overrides["provider"] = args.provider
    if args.model:
        overrides["model"] = args.model
    if args.base_url:
        overrides["base_url"] = args.base_url
    if args.workdir:
        overrides["workdir"] = Path(args.workdir)
    if args.mock:
        overrides["provider"] = "mock"
    if args.allow_all:
        overrides["approval"] = "allow"
    elif args.approval:
        overrides["approval"] = args.approval

    event_sink = _print if args.verbose else None
    progress_sink = None if args.quiet else _print_progress

    if args.demo:
        overrides["provider"] = "mock"
    script = _demo_script() if args.demo else None

    settings = load_settings(args.env_file, **{k: v for k, v in overrides.items() if k != "approval"})

    if args.status:
        _print(settings.describe())
        return 0

    if not settings.is_mock and not settings.api_key:
        _print(
            "\033[31mNo API key found.\033[0m Copy .env.example to .env and set "
            "ANTHROPIC_API_KEY, or run with --mock."
        )
        return 2

    runtime = Runtime.create(
        settings,
        script=script,
        # Only pass approval when the user actually asked for it on the command
        # line.  Passing a literal default here would shadow AGENT_APPROVAL in
        # .env, making that setting silently do nothing.
        **({"approval": overrides["approval"]} if "approval" in overrides else {}),
        teams_enabled=not args.no_teams,
        memory_enabled=not args.no_memory,
        compaction_enabled=not args.no_compaction,
        require_plan=not args.no_plan_gate,
        verbose=args.verbose,
        on_event=event_sink,
        on_progress=progress_sink,
    )

    try:
        if args.demo:
            _print("\033[36m[demo] running offline against the mock model\033[0m")
            answer = runtime.submit("Write a greeting module and verify it works.")
            _print()
            _print(answer)
            _print()
            _print(runtime.status())
            return 0

        if args.request:
            answer = runtime.submit(" ".join(args.request))
            answer = runtime.wait_for_teammates(initial_answer=answer)
            _print(answer)
            return 0

        return _repl(runtime)
    finally:
        runtime.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
