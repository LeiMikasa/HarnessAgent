"""Basic tools -- the action space.

    bash        run a shell command
    read_file   read a file
    write_file  create or overwrite a file
    edit_file   replace exact text once
    glob        find files by pattern
    grep        search file contents

Bash uses Docker when enabled. File tools run on the host with mode and
canonical-path checks; /workspace aliases refer to the same mounted files.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ..shell import decode_shell_output, noninteractive_process_options, shell_guidance, shell_name
from ..permissions import FULL, READ_ONLY, HOST_EXECUTION_KEY
from ..workspace_paths import host_workspace_path
from .registry import ToolContext, ToolRegistry
from .result import ToolError, ToolResult, tool_error
from .search import glob_paths, has_brace_expansion

# Caps.  These exist so one careless command cannot flood the context window.
MAX_TOOL_OUTPUT = 50_000  # 任何工具输出最多5万字符，超出截断
BASH_TIMEOUT_SECONDS = 120 # bash默认120秒超时
GLOB_MAX_MATCHES = 200 # glob最多返回200个匹配
GREP_MAX_MATCHES = 500 # grep最多返回500行匹配，另受总输出字符预算限制
GREP_MAX_FILE_BYTES = 2_000_000 # 跳过超2MB的文件
GREP_LINE_LIMIT = 1000 # 每条命中内容最多1000字符，围绕匹配位置显示


def _host_execution_allowed(args: dict, ctx: ToolContext) -> bool:
    """Only a trusted mode or permission hook can grant unrestricted host access."""
    approved = bool(ctx.extra.get(HOST_EXECUTION_KEY))
    full_mode = getattr(ctx.runtime, "permission_mode", None) == FULL
    if args.get("execution_mode") == "host" and not (approved or full_mode):
        raise ToolError("PERMISSION_DENIED", "Host execution requires approval for this invocation")
    return approved or full_mode


def _use_sandbox(args: dict, ctx: ToolContext) -> bool:
    return bool(not _host_execution_allowed(args, ctx) and getattr(ctx.runtime, "sandbox_enabled", False))


class WorkspaceEscapeError(ToolError, ValueError):
    """Raised when a path argument points outside the workspace."""

    def __init__(self, message: str, code: str = "PERMISSION_DENIED"):
        super().__init__(code, message, field="path",
                         action="correct_arguments" if code == "INVALID_ARGUMENT" else "report")

# 唯一的收窄点
def safe_path(ctx: ToolContext, raw: str, *, execution_mode: str = "default") -> Path:
    """Resolve `raw` against the context workspace and contain it.

    Containment is enforced here rather than in the permission rules, so an
    unapproved escape is impossible even if a caller skips the hook system.
    An approved escape sets `ctx.extra["allow_outside"]`.
    """
    root = Path(ctx.workdir).resolve()
    if not isinstance(raw, str) or not raw.strip():
        raise WorkspaceEscapeError("path is required", "INVALID_ARGUMENT")

    raw = host_workspace_path(raw, root,
                              sandbox_enabled=bool(getattr(ctx.runtime, "sandbox_enabled", False)),
                              execution_mode=execution_mode)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute(): # 是绝对路径吗
        candidate = root / candidate
    try:
        candidate = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise WorkspaceEscapeError(f"cannot resolve path {raw!r}: {exc}", "INVALID_ARGUMENT") from exc

    if candidate != root and not candidate.is_relative_to(root):
        if not ctx.extra.get("allow_outside") and getattr(ctx.runtime, "permission_mode", None) != FULL:
            raise WorkspaceEscapeError(f"path escapes workspace: {raw}")
    return candidate


def _check_file_access(args: dict, ctx: ToolContext, *, mutation: bool = False) -> None:
    """Defend file handlers even when a caller bypasses the permission hook."""
    _host_execution_allowed(args, ctx)  # Validate explicit escalation; file I/O stays on host.
    if (mutation and getattr(ctx.runtime, "permission_mode", None) == READ_ONLY
            and not ctx.extra.get(HOST_EXECUTION_KEY)):
        raise ToolError("PERMISSION_DENIED", "Read-only mode forbids file modifications")


# --------------------------------------------------------------------------
# bash
# --------------------------------------------------------------------------

# 关键告诉模型还差多少
def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text    #为什么这个数字有用？ 因为模型看到"还差 800000 字符"就知道"我不该整个读，应该用 offset/limit 分批读"或者"应该用 grep 精确定位"。如果只说"已截断"，模型可能会再试一次同样的读法。
    return text[:limit] + f"\n... (truncated, {len(text) - limit} more characters)"


def run_bash(args: dict, ctx: ToolContext) -> str:
    command = args.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return tool_error("INVALID_ARGUMENT", "command is required", field="command", action="correct_arguments")

    timeout = args.get("timeout")
    timeout = BASH_TIMEOUT_SECONDS if not isinstance(timeout, (int, float)) else float(timeout)

    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    # try:
    #     result = subprocess.run(
    #         command,
    #         shell=True,
    #         cwd=str(ctx.workdir),
    #         capture_output=True, # 捕获 stdout+stderr
    #         env=child_env,
    #         timeout=timeout,
    #     )
    # except subprocess.TimeoutExpired:
    #     return tool_error("TIMEOUT", f"Timeout ({timeout:g}s); command may have produced side effects. "
    #                       "Inspect the actual state before issuing another command.",
    #                       action="inspect_state", execution_status="unknown")
    # except (FileNotFoundError, OSError) as exc:
    #     return ToolResult.from_exception(exc)
    try:
        runtime=ctx.runtime
        if _use_sandbox(args, ctx):
            # 沙箱模式：在容器里执行
            sandbox=runtime.get_sandbox(ctx.workdir)
            result=sandbox.execute(command,timeout=timeout)
        else:
            result=subprocess.run(
                command,
                shell=True,
                cwd=str(ctx.workdir),
                capture_output=True,
                env=child_env,
                timeout=timeout,
                **noninteractive_process_options(),
            )
    except (subprocess.TimeoutExpired, TimeoutError):
        return tool_error(
            "TIMEOUT",
            f"Timeout ({timeout:g}s); inspect actual state before retrying.",
            action="inspect_state",
            execution_status="unknown",
        )

    except ToolError as exc:
        return ToolResult.from_exception(exc)
    except (OSError, RuntimeError, ValueError) as exc:
        return ToolResult.from_exception(exc)
    stdout = decode_shell_output(result.stdout).strip()
    stderr = decode_shell_output(result.stderr).strip()
    if result.returncode:
        # Reserve room for both streams so a verbose stdout cannot hide stderr.
        budget = MAX_TOOL_OUTPUT - 300
        out_budget = min(len(stdout), budget // 2)
        err_budget = min(len(stderr), budget - out_budget)
        out_budget = min(len(stdout), budget - err_budget)
        output = (f"Exit code: {result.returncode}\nstdout:\n{_clip(stdout, out_budget) or '(empty)'}"
                  f"\nstderr:\n{_clip(stderr, err_budget) or '(empty)'}")
        return tool_error("COMMAND_FAILED", output,
                          action="inspect_state", execution_status="unknown")
    output = "\n".join(part for part in (stdout, stderr) if part)
    return _clip(output) if output else "(no output)"


# --------------------------------------------------------------------------
# read_file
# --------------------------------------------------------------------------


def run_read_file(args: dict, ctx: ToolContext) -> str:
    _check_file_access(args, ctx)
    path = safe_path(ctx, args.get("path", ""), execution_mode=args.get("execution_mode", "default"))
    try:
        text = ctx.files.read(path)
    except FileNotFoundError:
        return tool_error("NOT_FOUND", f"no such file: {args.get('path')}", action="inspect",
                          inspected_path=os.path.normcase(str(path)))
    except OSError as exc:
        return ToolResult.from_exception(exc, read_only=True)
    except ToolError as exc:
        return ToolResult.from_exception(exc, read_only=True)

    offset = args.get("offset")
    limit = args.get("limit")
    lines = text.splitlines()

    start = max(0, int(offset) - 1) if isinstance(offset, int) and offset > 0 else 0
    end = len(lines)
    if isinstance(limit, int) and limit > 0:
        end = min(len(lines), start + limit) # min 防越界。如果 start=100, limit=50 但文件只有 120 行，end 会是 120 而不是 150。切片越界不报错，但显式 min 让后面的"还有多少行"计算准确。

    window = lines[start:end]
    body = "\n".join(window)
    notes = []
    if start > 0:
        notes.append(f"started at line {start + 1}")
    if end < len(lines):
        notes.append(f"{len(lines) - end} more lines")
    if notes:
        body += "\n... (" + "; ".join(notes) + ")"
    # Only a complete, untruncated read is evidence for resolving uncertain writes.
    inspected = os.path.normcase(str(path)) if start == 0 and end == len(lines) and len(body) <= MAX_TOOL_OUTPUT else None
    return ToolResult(_clip(body) if body else "(empty file)", inspected_path=inspected)


# --------------------------------------------------------------------------
# write_file
# --------------------------------------------------------------------------


def run_write_file(args: dict, ctx: ToolContext) -> str:
    _check_file_access(args, ctx, mutation=True)
    path = safe_path(ctx, args.get("path", ""), execution_mode=args.get("execution_mode", "default"))
    content = args.get("content", "")
    if not isinstance(content, str):
        return tool_error("INVALID_ARGUMENT", "content must be a string", field="content", action="correct_arguments")
    try:
        ctx.files.write(path, content)
    except (OSError, ToolError) as exc:
        return ToolResult.from_exception(exc)
    return f"Wrote {len(content)} bytes to {args.get('path')}"


# --------------------------------------------------------------------------
# edit_file
# --------------------------------------------------------------------------


def run_edit_file(args: dict, ctx: ToolContext) -> str:
    _check_file_access(args, ctx, mutation=True)
    path = safe_path(ctx, args.get("path", ""), execution_mode=args.get("execution_mode", "default"))
    old_text = args.get("old_text")
    new_text = args.get("new_text")

    if not isinstance(old_text, str) or not old_text:
        return tool_error("INVALID_ARGUMENT", "old_text is required", field="old_text", action="correct_arguments")
    if not isinstance(new_text, str):
        return tool_error("INVALID_ARGUMENT", "new_text is required", field="new_text", action="correct_arguments")
    try:
        replaced = ctx.files.edit(path, old_text, new_text, replace_all=bool(args.get("replace_all")))
    except (OSError, ToolError) as exc:
        return ToolResult.from_exception(exc)

    return f"Edited {args.get('path')} ({replaced} replacement{'s' if replaced != 1 else ''})"


# --------------------------------------------------------------------------
# glob
# --------------------------------------------------------------------------


def run_glob(args: dict, ctx: ToolContext) -> str:
    _check_file_access(args, ctx)
    pattern = args.get("pattern", "")
    if not isinstance(pattern, str) or not pattern.strip():
        return tool_error("INVALID_ARGUMENT", "pattern is required", field="pattern", action="correct_arguments")
    if has_brace_expansion(pattern):
        return tool_error("INVALID_ARGUMENT", "Brace expansion is unsupported; use separate glob patterns.",
                          field="pattern", action="correct_arguments")

    root = Path(ctx.workdir).resolve()
    pattern = host_workspace_path(pattern, root,
                                  sandbox_enabled=bool(getattr(ctx.runtime, "sandbox_enabled", False)),
                                  execution_mode=args.get("execution_mode", "default"))
    # Check traversal before glob starts walking host directories.
    prefix = []
    for part in Path(pattern).expanduser().parts:
        if any(char in part for char in "*?["):
            break
        prefix.append(part)
    safe_path(ctx, str(Path(*prefix)) if prefix else ".", execution_mode="host")
    unrestricted = bool(ctx.extra.get("allow_outside") or getattr(ctx.runtime, "permission_mode", None) == FULL)
    if ".." in Path(pattern).parts and not unrestricted:
        raise WorkspaceEscapeError("parent traversal in glob requires approval")
    try:
        raw_matches = glob_paths(root, pattern, include_ignored=bool(args.get("include_ignored")),
                                 allow_outside=unrestricted)
    except (re.error, OSError, ValueError) as exc:
        return ToolResult.from_exception(exc, read_only=True)

    matches: set[str] = set()
    for match in raw_matches:
        try:
            resolved = (root / match).resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_relative_to(root) or unrestricted:
            matches.add(match.as_posix())

    ordered = sorted(matches)
    shown = ordered[:GLOB_MAX_MATCHES]
    if len(ordered) > GLOB_MAX_MATCHES:
        shown.append(f"... ({len(ordered) - GLOB_MAX_MATCHES} more matches; narrow the pattern)")
    return "\n".join(shown) if shown else "(no matches)"


# --------------------------------------------------------------------------
# grep
# --------------------------------------------------------------------------


def _iter_files(root: Path, include: str | None, *, include_ignored: bool = False) -> list[Path]:
    pattern = include or "**/*"
    found = []
    for match in glob_paths(root, pattern, include_ignored=include_ignored):
        candidate = root / match
        try:
            if candidate.is_file() and candidate.resolve().is_relative_to(root):
                found.append(candidate)
        except (OSError, RuntimeError):
            continue
    return sorted(found)


def _grep_preview(line: str, match: re.Match[str]) -> str:
    """Keep the first match visible even when it occurs deep in a JSONL line."""
    if len(line) <= GREP_LINE_LIMIT:
        return line
    # Reserve space for both omission markers, with some context before the hit.
    window = GREP_LINE_LIMIT - 6
    start = max(0, min(match.start() - 40, len(line) - window))
    end = start + window
    return ("..." if start else "") + line[start:end] + ("..." if end < len(line) else "")


def run_grep(args: dict, ctx: ToolContext) -> str:
    _check_file_access(args, ctx)
    pattern = args.get("pattern", "")
    if not isinstance(pattern, str) or not pattern:
        return tool_error("INVALID_ARGUMENT", "pattern is required", field="pattern", action="correct_arguments")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return tool_error("INVALID_ARGUMENT", f"invalid regex: {exc}", field="pattern", action="correct_arguments")

    include = args.get("include")
    include = include if isinstance(include, str) and include.strip() else None
    if include and has_brace_expansion(include):
        return tool_error("INVALID_ARGUMENT", "Brace expansion is unsupported; use separate include patterns.",
                          field="include", action="correct_arguments")
    path_arg = args.get("path")
    root = safe_path(ctx, path_arg, execution_mode=args.get("execution_mode", "default")) if path_arg else Path(ctx.workdir).resolve()
    if not root.exists():
        return tool_error("NOT_FOUND", f"no such path: {path_arg}", action="inspect")
    # 如果root指向文件直接搜他，如果不是去迭代
    files = [root] if root.is_file() else _iter_files(root, include, include_ignored=bool(args.get("include_ignored")))
    results: list[str] = []
    truncated = False
    output_chars = 0
    truncation_note = (
        f"... (results limited to {GREP_MAX_MATCHES} matching lines or "
        f"{MAX_TOOL_OUTPUT} characters; narrow the pattern or search a specific file)"
    )
    result_budget = MAX_TOOL_OUTPUT - len(truncation_note) - 1

    for file in files:
        if truncated:
            break
        try:
            if file.stat().st_size > GREP_MAX_FILE_BYTES:
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        if "\x00" in text[:1024]:
            continue

        try:
            relative = file.resolve().relative_to(Path(ctx.workdir).resolve()).as_posix()
        except ValueError:
            relative = file.as_posix()

        for number, line in enumerate(text.splitlines(), start=1):
            match = regex.search(line)
            if match is not None:
                row = f"{relative}:{number}:{_grep_preview(line, match)}"
                row_chars = len(row) + (1 if results else 0)
                if len(results) >= GREP_MAX_MATCHES or output_chars + row_chars > result_budget:
                    truncated = True
                    break
                results.append(row)
                output_chars += row_chars

    if not results and not truncated:
        return "(no matches)"
    if truncated:
        results.append(truncation_note)
    return "\n".join(results)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

_BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "minLength": 1, "description": "Shell command to run."},
        "timeout": {"type": "number", "exclusiveMinimum": 0, "description": "Seconds before the command is killed."},
    },
    "required": ["command"],
}

_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "description": "File path, relative to the workspace."},
        "offset": {"type": "integer", "minimum": 1, "description": "First line to read (1-based)."},
        "limit": {"type": "integer", "minimum": 1, "description": "Maximum number of lines."},
    },
    "required": ["path"],
}

_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
}

_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "old_text": {"type": "string", "minLength": 1, "description": "Exact text to replace; must be unique."},
        "new_text": {"type": "string", "description": "Replacement text."},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
    },
    "required": ["path", "old_text", "new_text"],
}

_GLOB_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "minLength": 1, "description": "Glob pattern; ** is recursive. Brace expansion is unsupported."},
        "include_ignored": {"type": "boolean", "default": False,
                            "description": "Search dependency/build/hidden directories too. Explicit directory names in the pattern also opt in."},
    },
    "required": ["pattern"],
}

_GREP_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "minLength": 1, "description": "Python regular expression."},
        "include": {"type": "string", "description": "Glob filter, e.g. '**/*.py'."},
        "path": {"type": "string", "description": "File or directory to search."},
        "include_ignored": {"type": "boolean", "default": False,
                            "description": "Search dependency/build/hidden directories too. Explicit path or include directory names also opt in."},
    },
    "required": ["pattern"],
}


def register_basic_tools(registry: ToolRegistry,*,sandbox_enabled: bool=False) -> ToolRegistry:
    """Add the six base tools to `registry`."""
    for schema in (_BASH_SCHEMA, _READ_SCHEMA, _WRITE_SCHEMA, _EDIT_SCHEMA, _GLOB_SCHEMA, _GREP_SCHEMA):
        schema["properties"].update({
            "execution_mode": {
                "type": "string", "enum": ["default", "host"],
                "description": "default follows session boundaries; host requests one unrestricted invocation, requiring approval in restricted modes.",
            },
            "justification": {"type": "string", "minLength": 1,
                              "description": "Explain why this invocation requires wider file or command permissions."},
        })
        schema["allOf"] = [{
            "if": {"properties": {"execution_mode": {"const": "host"}}, "required": ["execution_mode"]},
            "then": {"required": ["justification"]},
        }]
        schema["additionalProperties"] = False
    registry.add(
        "bash",
        "Run a shell command in the workspace for Git, dependency installation, tests, builds and programs. "
        "Use dedicated file tools for file discovery, content search, text reading and modifications. "
        "Do not use shell cat/sed/head/tail/type/Get-Content to inspect text files, find/ls/dir to "
        "discover files, or shell rg/grep/findstr/Select-String to search file contents. Do not bypass "
        "these requirements with scripts or host execution. "
        + shell_guidance(sandbox_enabled=sandbox_enabled),
        _BASH_SCHEMA,
        run_bash,
    )
    registry.add("read_file", "Required tool for reading text files; do not use shell cat, sed, head, tail, type, Get-Content or scripts. "
                 "Use offset and limit for a specific line range or to continue a large file.",
                 _READ_SCHEMA, run_read_file, read_only=True, retry_safe=True)
    registry.add("write_file", "Create a file or intentionally overwrite its entire contents. "
                 "Before overwriting an existing file, read its current contents with the text-reading tool. "
                 "Prior observation and version checks are enforced; a successful create/edit also establishes observation. "
                 "On FS_NOT_OBSERVED or FS_STALE_VERSION, read_file again before retrying. "
                 "Prefer the available edit tool for small targeted changes.", _WRITE_SCHEMA, run_write_file)
    registry.add("edit_file", "Make targeted exact-text changes instead of shell replacement commands or scripts. "
                 "Read the current text first unless this session just created or edited it; use a unique match or replace_all. "
                 "Prior observation and version checks are enforced; re-read after FS_NOT_OBSERVED or FS_STALE_VERSION.", _EDIT_SCHEMA, run_edit_file)
    registry.add("glob", "Required tool for discovering file paths; do not use shell find, ls or dir. "
                 "Skips dependency/build directories (node_modules, .venv, .git, dist, build, caches) by default; "
                 "name a directory explicitly or set include_ignored=true to search it. "
                 "Supports *, ?, [] and recursive **, not brace expansion such as *.{ts,vue}; use separate patterns.",
                 _GLOB_SCHEMA, run_glob, read_only=True, retry_safe=True)
    registry.add("grep", "Required tool for searching file contents with a regular expression; do not use shell grep, rg, findstr or Select-String. "
                 "Skips dependency/build directories by default; use an explicit path/include or include_ignored=true to search them. "
                 "Returns up to 500 matching lines, "
                 "each with its file, line number and an excerpt of up to 1000 characters around the first match. "
                 "Output is capped at 50000 characters; use read_file for full context.",
                 _GREP_SCHEMA, run_grep, read_only=True, retry_safe=True)
    return registry


def shell_hint() -> str:
    """A short note about the shell the agent is driving."""
    return shell_name()
