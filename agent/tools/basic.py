"""Basic tools -- the action space.

    bash        run a shell command
    read_file   read a file
    write_file  create or overwrite a file
    edit_file   replace exact text once
    glob        find files by pattern
    grep        search file contents

Everything is workspace-scoped.  `safe_path` is the single choke point: it
resolves a caller-supplied path and refuses to leave the workspace unless the
permission pipeline explicitly approved an escape for this one call.
"""

from __future__ import annotations

import glob as globlib
import os
import re
import subprocess
import sys
from pathlib import Path

from .registry import ToolContext, ToolRegistry
from .result import ToolError, ToolResult, tool_error

# Caps.  These exist so one careless command cannot flood the context window.
MAX_TOOL_OUTPUT = 50_000  # 任何工具输出最多5万字符，超出截断
BASH_TIMEOUT_SECONDS = 120 # bash默认120秒超时
GLOB_MAX_MATCHES = 200 # glob最多返回200个匹配
GREP_MAX_MATCHES = 200 # grep最多返回200行匹配
GREP_MAX_FILE_BYTES = 2_000_000 # 跳过超2MB的文件
GREP_LINE_LIMIT = 400 # 每行最多显示400字符


class WorkspaceEscapeError(ToolError, ValueError):
    """Raised when a path argument points outside the workspace."""

    def __init__(self, message: str, code: str = "PERMISSION_DENIED"):
        super().__init__(code, message, field="path",
                         action="correct_arguments" if code == "INVALID_ARGUMENT" else "report")

# 唯一的收窄点
def safe_path(ctx: ToolContext, raw: str) -> Path:
    """Resolve `raw` against the context workspace and contain it.

    Containment is enforced here rather than in the permission rules, so an
    unapproved escape is impossible even if a caller skips the hook system.
    An approved escape sets `ctx.extra["allow_outside"]`.
    """
    root = Path(ctx.workdir).resolve()
    if not isinstance(raw, str) or not raw.strip():
        raise WorkspaceEscapeError("path is required", "INVALID_ARGUMENT")

    candidate = Path(raw).expanduser() # 绝对路径
    if not candidate.is_absolute(): # 是绝对路径吗
        candidate = root / candidate
    try:
        candidate = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise WorkspaceEscapeError(f"cannot resolve path {raw!r}: {exc}", "INVALID_ARGUMENT") from exc

    if candidate != root and not candidate.is_relative_to(root):
        if not ctx.extra.get("allow_outside"):
            raise WorkspaceEscapeError(f"path escapes workspace: {raw}")
    return candidate


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

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(ctx.workdir),
            capture_output=True, # 捕获 stdout+stderr
            text=True,   # 文本模式
            errors="replace",  # 解码错误用 ? 替换
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return tool_error("TIMEOUT", f"Timeout ({timeout:g}s); command may have produced side effects. "
                          "Inspect the actual state before issuing another command.",
                          action="inspect_state", execution_status="unknown")
    except (FileNotFoundError, OSError) as exc:
        return ToolResult.from_exception(exc)

    output = (result.stdout + result.stderr).strip()
    output = _clip(output) if output else "(no output)"
    if result.returncode:
        return tool_error("COMMAND_FAILED", f"command exited with status {result.returncode}\n{output}",
                          action="inspect_state", execution_status="unknown")
    return output


# --------------------------------------------------------------------------
# read_file
# --------------------------------------------------------------------------


def run_read_file(args: dict, ctx: ToolContext) -> str:
    path = safe_path(ctx, args.get("path", ""))
    try:
        path.stat()
    except FileNotFoundError:
        return tool_error("NOT_FOUND", f"no such file: {args.get('path')}", action="inspect",
                          inspected_path=os.path.normcase(str(path)))
    except OSError as exc:
        return ToolResult.from_exception(exc, read_only=True)
    if path.is_dir():
        return tool_error("PRECONDITION_FAILED", f"{args.get('path')} is a directory; use glob", action="inspect")

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
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
    path = safe_path(ctx, args.get("path", ""))
    content = args.get("content", "")
    if not isinstance(content, str):
        return tool_error("INVALID_ARGUMENT", "content must be a string", field="content", action="correct_arguments")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolResult.from_exception(exc)
    return f"Wrote {len(content)} bytes to {args.get('path')}"


# --------------------------------------------------------------------------
# edit_file
# --------------------------------------------------------------------------


def run_edit_file(args: dict, ctx: ToolContext) -> str:
    path = safe_path(ctx, args.get("path", ""))
    old_text = args.get("old_text")
    new_text = args.get("new_text")

    if not isinstance(old_text, str) or not old_text:
        return tool_error("INVALID_ARGUMENT", "old_text is required", field="old_text", action="correct_arguments")
    if not isinstance(new_text, str):
        return tool_error("INVALID_ARGUMENT", "new_text is required", field="new_text", action="correct_arguments")
    if not path.is_file():
        return tool_error("NOT_FOUND", f"no such file: {args.get('path')}", action="inspect")

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult.from_exception(exc, read_only=True)

    count = content.count(old_text)
    replace_all = bool(args.get("replace_all"))

    if count == 0:
        return tool_error("PRECONDITION_FAILED", f"old_text not found in {args.get('path')}; "
                          "read_file first, then use the actual unique text.", action="inspect")
    if count > 1 and not replace_all:
        return tool_error("PRECONDITION_FAILED", f"old_text appears {count} times in {args.get('path')}; "
                          "read_file and add more surrounding context to make it unique, or pass replace_all",
                          action="inspect")

    updated = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return ToolResult.from_exception(exc)

    replaced = count if replace_all else 1
    return f"Edited {args.get('path')} ({replaced} replacement{'s' if replaced != 1 else ''})"


# --------------------------------------------------------------------------
# glob
# --------------------------------------------------------------------------


def run_glob(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern", "")
    if not isinstance(pattern, str) or not pattern.strip():
        return tool_error("INVALID_ARGUMENT", "pattern is required", field="pattern", action="correct_arguments")

    root = Path(ctx.workdir).resolve()
    try:
        raw_matches = globlib.glob(pattern, root_dir=str(root), recursive=True)
    except (re.error, OSError, ValueError) as exc:
        return ToolResult.from_exception(exc, read_only=True)

    matches: set[str] = set()
    for match in raw_matches:
        try:
            resolved = (root / match).resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_relative_to(root):
            matches.add(match.replace("\\", "/"))

    ordered = sorted(matches)
    shown = ordered[:GLOB_MAX_MATCHES]
    if len(ordered) > GLOB_MAX_MATCHES:
        shown.append(f"... ({len(ordered) - GLOB_MAX_MATCHES} more matches; narrow the pattern)")
    return "\n".join(shown) if shown else "(no matches)"


# --------------------------------------------------------------------------
# grep
# --------------------------------------------------------------------------


def _iter_files(root: Path, include: str | None) -> list[Path]:
    pattern = include or "**/*"
    found = []
    for match in globlib.glob(pattern, root_dir=str(root), recursive=True):
        candidate = root / match
        try:
            if candidate.is_file() and candidate.resolve().is_relative_to(root):
                found.append(candidate)
        except (OSError, RuntimeError):
            continue
    return sorted(found)


def run_grep(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern", "")
    if not isinstance(pattern, str) or not pattern:
        return tool_error("INVALID_ARGUMENT", "pattern is required", field="pattern", action="correct_arguments")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return tool_error("INVALID_ARGUMENT", f"invalid regex: {exc}", field="pattern", action="correct_arguments")

    include = args.get("include")
    include = include if isinstance(include, str) and include.strip() else None
    path_arg = args.get("path")
    root = safe_path(ctx, path_arg) if path_arg else Path(ctx.workdir).resolve()
    if not root.exists():
        return tool_error("NOT_FOUND", f"no such path: {path_arg}", action="inspect")
    # 如果root指向文件直接搜他，如果不是去迭代
    files = [root] if root.is_file() else _iter_files(root, include)
    results: list[str] = []
    truncated = False

    for file in files:
        if len(results) >= GREP_MAX_MATCHES:
            truncated = True
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
            if regex.search(line):
                results.append(f"{relative}:{number}:{line[:GREP_LINE_LIMIT]}")
                if len(results) >= GREP_MAX_MATCHES:
                    truncated = True
                    break

    if not results:
        return "(no matches)"
    if truncated:
        results.append(f"... (stopped at {GREP_MAX_MATCHES} matches; narrow the pattern)")
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
    "properties": {"pattern": {"type": "string", "minLength": 1, "description": "Glob pattern; ** is recursive."}},
    "required": ["pattern"],
}

_GREP_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "minLength": 1, "description": "Python regular expression."},
        "include": {"type": "string", "description": "Glob filter, e.g. '**/*.py'."},
        "path": {"type": "string", "description": "File or directory to search."},
    },
    "required": ["pattern"],
}


def register_basic_tools(registry: ToolRegistry) -> ToolRegistry:
    """Add the six base tools to `registry`."""
    for schema in (_BASH_SCHEMA, _READ_SCHEMA, _WRITE_SCHEMA, _EDIT_SCHEMA, _GLOB_SCHEMA, _GREP_SCHEMA):
        schema["additionalProperties"] = False
    registry.add("bash", "Run a shell command in the workspace.", _BASH_SCHEMA, run_bash)
    registry.add("read_file", "Read a file's contents.", _READ_SCHEMA, run_read_file, read_only=True, retry_safe=True)
    registry.add("write_file", "Create or overwrite a file.", _WRITE_SCHEMA, run_write_file)
    registry.add("edit_file", "Replace exact text in a file.", _EDIT_SCHEMA, run_edit_file)
    registry.add("glob", "Find files by glob pattern.", _GLOB_SCHEMA, run_glob, read_only=True, retry_safe=True)
    registry.add("grep", "Search file contents with a regular expression.", _GREP_SCHEMA, run_grep, read_only=True, retry_safe=True)
    return registry


def shell_hint() -> str:
    """A short note about the shell the agent is driving."""
    return "cmd.exe" if sys.platform == "win32" else "/bin/sh"
