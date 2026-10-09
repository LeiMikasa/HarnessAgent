"""MCP plugin layer -- external capability routed into the same tool pool.

    connect_mcp("docs")
              |
              v
    +------------------+     tools/list     +------------------+
    | Agent Harness    | <----------------- | MCP server       |
    | built-in tools   |     tools/call     | docs             |
    | + MCP tools      | -----------------> | search, version  |
    +--------+---------+                    +------------------+
             |
             v
    +-------------------------------------------------------------+
    | bash | read_file | ... | mcp__docs__search | mcp__deploy__x |
    +-------------------------------------------------------------+

Two things make this safe and boring at the same time:

  * **Namespacing.** A server's `search` becomes `mcp__docs__search`, so two
    servers may both expose `search` without colliding, and the model can see
    where a tool came from.
  * **Host-side authorization.** A server's own description of a tool is
    untrusted input.  Whether a tool may run unattended is decided by
    `MCP_HOST_POLICY` on the host, never by the server's `annotations`.

Every MCP tool lands in the same `ToolRegistry` as the built-ins, so the agent
loop needs no knowledge that MCP exists.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .tools.registry import Tool, ToolContext, ToolRegistry
from .tools.result import ToolError, ToolResult, tool_error

MAX_TOOL_NAME_CHARS = 64        # 工具名最长 64 个字符。
MAX_MCP_OUTPUT_CHARS = 50_000   # MCP 工具输出最多 50000 字符，避免一次返回把上下文炸掉。
CALL_TIMEOUT_SECONDS = 60.0     # 调用超时 60 秒。
DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")  # 只允许字母、数字、下划线、连字符，其他字符会被替换。


class MCPError(ToolError, RuntimeError):
    """Raised for protocol, transport, or configuration failures."""

    def __init__(self, message: str, *, code: str = "MCP_ERROR", transient: bool = False):
        super().__init__(code, message, transient=transient,
                         execution_status="unknown", action="inspect_state")


def normalize_mcp_name(name: str) -> str:  # 把服务器名或工具名映射到模型工具名允许的字符集
    """Map a name into the alphabet model tool names allow."""
    normalized = DISALLOWED_CHARS.sub("_", str(name))
    if not normalized:
        raise MCPError("MCP names cannot normalize to an empty string")
    return normalized


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------


class MCPClient:
    """Base class: a source of named tools."""

    def __init__(self, name: str):
        self.name = name

    def list_tools(self) -> list[dict]:
        raise NotImplementedError

    def call_tool(self, tool_name: str, args: dict) -> str:
        raise NotImplementedError

    def close(self) -> None:
        return None


class InProcessMCPClient(MCPClient):
    """An MCP server implemented in Python, in this process.

    This is the shape the course uses, and it is genuinely useful: it is how
    you wrap an internal API as agent tools without shipping a server.
    """

    def __init__(self, name: str):
        super().__init__(name)
        self.tools: list[dict] = []
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register(self, tool_defs: list[dict], handlers: dict[str, Callable[..., Any]]) -> None:
        names = [tool.get("name") for tool in tool_defs]
        if any(not isinstance(name, str) or not name for name in names):
            raise MCPError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise MCPError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise MCPError(f"Missing MCP handlers: {', '.join(missing)}")
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def list_tools(self) -> list[dict]:
        return list(self.tools)

    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return tool_error("UNKNOWN_TOOL", f"MCP error: unknown tool {tool_name!r}", action="correct_arguments")
        try:
            output = handler(**args)
            return output if isinstance(output, ToolResult) else str(output)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.from_exception(exc)


class StdioMCPClient(MCPClient):
    """A real MCP server driven over newline-delimited JSON-RPC on stdio.

    A background reader thread decodes responses into a queue keyed by request
    id, which keeps the transport portable (no `select` on pipes, which does
    not work on Windows).
    """

    def __init__(self, name: str, command: list[str], *, env: dict | None = None, cwd: Path | None = None):
        super().__init__(name)
        self.command = [str(part) for part in command] # 启动 MCP 服务器的命令
        self._env = {**os.environ, **(env or {})}
        self._cwd = str(cwd) if cwd else None
        self._process: subprocess.Popen | None = None
        self._responses: queue.Queue[dict] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._tools: list[dict] = []
        self._errors: list[str] = []

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self._env,
                cwd=self._cwd,
            )
        except (OSError, ValueError) as exc:
            raise MCPError(f"Could not start MCP server {self.name!r}: {exc}") from exc

        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-{self.name}", daemon=True
        )
        self._reader.start()

        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "harnessagent", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized", {})
        self._tools = self._extract_tools(self._request("tools/list", {}))

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass

    # -- transport ----------------------------------------------------------

    def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._errors.append(line[:200])
                continue
            if isinstance(message, dict):
                self._responses.put(message)
        # stdout closed: wake any waiter so it does not hang until timeout.
        self._responses.put({"__eof__": True})

    def _send(self, payload: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise MCPError(f"MCP server {self.name!r} is not running", code="CONNECTION_ERROR", transient=True)
        with self._lock:
            try:
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise MCPError(f"MCP server {self.name!r} closed the pipe: {exc}", code="CONNECTION_ERROR", transient=True) from exc

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float = CALL_TIMEOUT_SECONDS) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

        deadline = timeout
        while deadline > 0:
            try:
                message = self._responses.get(timeout=deadline)
            except queue.Empty:
                raise MCPError(f"MCP server {self.name!r} timed out on {method}", code="TIMEOUT", transient=True)
            if message.get("__eof__"):
                raise MCPError(f"MCP server {self.name!r} exited during {method}", code="CONNECTION_ERROR", transient=True)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise MCPError(f"MCP server {self.name!r} error on {method}: {message['error']}")
            return message.get("result") or {}
        raise MCPError(f"MCP server {self.name!r} timed out on {method}", code="TIMEOUT", transient=True)

    # -- protocol -----------------------------------------------------------

    @staticmethod
    def _extract_tools(result: dict) -> list[dict]:
        tools = result.get("tools")
        return [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []

    def list_tools(self) -> list[dict]:
        if self._process is None:
            self.start()
        return list(self._tools)

    def call_tool(self, tool_name: str, args: dict) -> str:
        if self._process is None:
            self.start()
        try:
            result = self._request(
                "tools/call", {"name": tool_name, "arguments": args}, timeout=CALL_TIMEOUT_SECONDS
            )
        except MCPError as exc:
            return ToolResult.from_exception(exc)

        parts: list[str] = []
        for block in result.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        output = "\n".join(part for part in parts if part).strip() or "(no output)"
        if result.get("isError"):
            return tool_error("REMOTE_TOOL_ERROR", f"MCP tool error: {output[:MAX_MCP_OUTPUT_CHARS]}",
                              action="inspect_state", execution_status="unknown")
        return output[:MAX_MCP_OUTPUT_CHARS]


# --------------------------------------------------------------------------
# Host policy
# --------------------------------------------------------------------------

#: Authorization comes from host configuration, never from server metadata.
#: "allow" runs unattended; anything else goes through the approval gate.
DEFAULT_HOST_POLICY: dict[tuple[str, str], str] = {}


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------


@dataclass
class _Connected:
    client: MCPClient
    tools: list[dict] = field(default_factory=list)


class MCPManager:
    """Owns connected servers and turns their tools into registry entries."""

    def __init__(
        self,
        *,
        policy: dict[tuple[str, str], str] | None = None,
        retry_safe_tools: Iterable[tuple[str, str]] = (),
        verbose: bool = False,
    ):
        self.policy = dict(DEFAULT_HOST_POLICY)
        self.retry_safe_tools = set(retry_safe_tools)
        if policy:
            self.policy.update(policy)
        self.servers: dict[str, _Connected] = {}
        self.verbose = verbose
        self.notes: list[str] = []

    # -- connection ---------------------------------------------------------

    def connect(self, client: MCPClient) -> str:
        name = client.name
        if name in self.servers:
            return f"MCP server {name!r} is already connected"
        try:
            tools = client.list_tools()
        except Exception as exc:  # noqa: BLE001
            client.close()
            return ToolResult.failure("MCP_CONNECTION_FAILED", f"MCP error: could not list tools from {name!r}: {type(exc).__name__}: {exc}", execution_status="unknown")
        self.servers[name] = _Connected(client=client, tools=tools)
        self._note(f"connected {name!r} with {len(tools)} tool(s)")
        return f"Connected MCP server {name!r} ({len(tools)} tools)"

    def connect_stdio(self, name: str, command: list[str], **kwargs) -> str:
        client = StdioMCPClient(name, command, **kwargs)
        try:
            client.start()
        except MCPError as exc:
            client.close()
            return ToolResult.from_exception(exc)
        return self.connect(client)

    def disconnect(self, name: str) -> str:
        entry = self.servers.pop(name, None)
        if entry is None:
            return f"MCP server {name!r} is not connected"
        entry.client.close()
        self._note(f"disconnected {name!r}")
        return f"Disconnected MCP server {name!r}"

    def close_all(self) -> None:
        for name in list(self.servers):
            self.disconnect(name)

    def names(self) -> list[str]:
        return list(self.servers)

    # -- tool pool assembly -------------------------------------------------

    def assemble_tool_pool(
        self, reserved_names: Iterable[str] | None = None
    ) -> tuple[dict[str, Tool], dict[str, str]]:
        """Return `{prefixed_name: Tool}` plus the per-tool approval policy.

        `reserved_names` are host-owned tool names that an MCP tool must never
        shadow.  Prefixing already makes that nearly impossible, but the
        registry merge uses `replace=True`, so the guard is cheap insurance.

        Raises `MCPError` on a name collision, an over-long name, or a
        non-object schema -- a malformed server must fail loudly rather than
        silently shadow a built-in.
        """
        tools: dict[str, Tool] = {}
        policies: dict[str, str] = {}
        origins: dict[str, str] = {
            name: f"host tool {name!r}" for name in (reserved_names or ())
        }

        for server_name, entry in self.servers.items():
            safe_server = normalize_mcp_name(server_name)
            for tool_def in entry.tools:
                raw_name = str(tool_def.get("name", ""))
                if not raw_name:
                    continue
                safe_tool = normalize_mcp_name(raw_name)
                prefixed = f"mcp__{safe_server}__{safe_tool}"
                if len(prefixed) > MAX_TOOL_NAME_CHARS:
                    raise MCPError(f"MCP tool name is longer than {MAX_TOOL_NAME_CHARS} characters: {prefixed}")

                origin = f"MCP tool {server_name!r}/{raw_name!r}"
                if prefixed in origins:
                    raise MCPError(
                        f"MCP tool name collision after normalization: {prefixed!r} "
                        f"maps both {origins[prefixed]} and {origin}"
                    )

                schema = tool_def.get("inputSchema") or tool_def.get("input_schema") or {}
                if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                    raise MCPError(f"Invalid input schema for {origin}")

                origins[prefixed] = origin
                client = entry.client
                tools[prefixed] = Tool(
                    name=prefixed,
                    description=str(tool_def.get("description", "")),
                    input_schema=schema,
                    handler=_make_handler(client, raw_name),
                    source=f"mcp__{safe_server}",
                    read_only=(server_name, raw_name) in self.retry_safe_tools,
                    retry_safe=(server_name, raw_name) in self.retry_safe_tools,
                )
                policies[prefixed] = self.policy.get((server_name, raw_name), "confirm")

        return tools, policies

    # ------------------------------------------------------------------

    def _note(self, text: str) -> None:
        self.notes.append(text)
        if self.verbose:
            print(f"\033[36m[mcp] {text}\033[0m")

    def drain_notes(self) -> list[str]:
        notes, self.notes = self.notes, []
        return notes


def _make_handler(client: MCPClient, raw_name: str) -> Callable[[dict, ToolContext], str]:
    def handler(args: dict, ctx: ToolContext) -> str:
        return client.call_tool(raw_name, args)

    handler.__name__ = f"mcp_{raw_name}"
    return handler


# --------------------------------------------------------------------------
# Built-in demonstration servers
# --------------------------------------------------------------------------


def build_docs_server() -> InProcessMCPClient:
    """A tiny docs server, useful as a template and for tests."""
    server = InProcessMCPClient("docs")

    from . import __version__

    corpus = {
        "compaction": "Five stages: tool_result_budget, snip_compact, micro_compact, fit_tool_results, compact_history.",
        "permissions": "Three gates: deny list, rule matching, user approval.",
        "teams": "Persistent teammates, a file-backed mailbox, and one in-progress task per teammate.",
        "memory": "Selection, extraction, and consolidation over .agent/memory/*.md.",
        "mcp": "External tools are namespaced as mcp__<server>__<tool> and share one tool pool.",
    }

    def search(query: str) -> str:
        needle = str(query).lower()
        hits = [f"{key}: {value}" for key, value in corpus.items() if needle in key or needle in value.lower()]
        return "\n".join(hits) if hits else f"No documentation matches {query!r}"

    def get_version() -> str:
        return f"harnessagent {__version__}"

    def list_topics() -> str:
        return ", ".join(sorted(corpus))

    server.register(
        tool_defs=[
            {
                "name": "search",
                "description": "Search the harness documentation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                # Untrusted hint: authorization comes from the host policy.
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "get_version",
                "description": "Get the harness version.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "list_topics",
                "description": "List documentation topics.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={"search": search, "get_version": get_version, "list_topics": list_topics},
    )
    return server


def build_deploy_server() -> InProcessMCPClient:
    """A server with a genuinely consequential tool, to exercise the gate."""
    server = InProcessMCPClient("deploy")
    state = {"environment": "staging", "released": []}

    def status() -> str:
        return f"environment={state['environment']} released={state['released'] or 'nothing'}"

    def trigger(service: str, environment: str = "staging") -> str:
        state["environment"] = str(environment)
        state["released"].append(str(service))
        return f"Triggered deploy of {service} to {environment}"

    server.register(
        tool_defs=[
            {
                "name": "status",
                "description": "Show the current deploy status.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "trigger",
                "description": "Trigger a deployment. Requires approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string"},
                        "environment": {"type": "string"},
                    },
                    "required": ["service"],
                },
            },
        ],
        handlers={"status": status, "trigger": trigger},
    )
    return server


#: The servers `connect_mcp` knows by name out of the box.
BUILTIN_SERVERS: dict[str, Callable[[], MCPClient]] = {
    "docs": build_docs_server,
    "deploy": build_deploy_server,
}


# --------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------

CONNECT_MCP_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Name of the MCP server to connect (e.g. 'docs').",
        }
    },
    "required": ["name"],
}

CONNECT_MCP_DESCRIPTION = (
    "Connect an MCP server so its tools join the tool pool. Connect before "
    "calling any mcp__ tool. Connected servers are listed in the system prompt."
)


def _manager(ctx: ToolContext) -> MCPManager | None:
    runtime = ctx.runtime
    if runtime is not None and getattr(runtime, "mcp", None) is not None:
        return runtime.mcp
    return ctx.extra.get("mcp")


def run_connect_mcp(args: dict, ctx: ToolContext) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return ToolResult.failure('INVALID_ARGUMENT', "Error: name is required", action='correct_arguments', execution_status='not_executed')

    manager = _manager(ctx)
    if manager is None:
        return ToolResult.failure('TOOL_UNAVAILABLE', "Error: MCP is not enabled for this session", action='report', execution_status='not_executed')

    factory = BUILTIN_SERVERS.get(name)
    if factory is None:
        available = ", ".join(sorted(BUILTIN_SERVERS)) or "none"
        return ToolResult.failure('NOT_FOUND', f"Error: unknown MCP server {name!r}. Available: {available}", action='inspect', execution_status='not_executed')

    result = manager.connect(factory())

    # Refresh the pool so the newly discovered tools are callable immediately.
    runtime = ctx.runtime
    if runtime is not None and hasattr(runtime, "refresh_mcp_tools"):
        runtime.refresh_mcp_tools()
    return result


def register_mcp_tools(registry: ToolRegistry) -> ToolRegistry:
    registry.add(
        "connect_mcp", CONNECT_MCP_DESCRIPTION, CONNECT_MCP_SCHEMA, run_connect_mcp
    )
    return registry
