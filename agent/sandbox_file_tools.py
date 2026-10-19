"""Standard-library-only worker sent to Python inside the Docker container.

The host sends this fixed source with ``python -I -c`` and arguments as JSON on
stdin. No agent package, credentials, or model client is installed in Docker.
"""

from __future__ import annotations

import glob as globlib
import json
import re
import sys
from pathlib import Path

MAX_TOOL_OUTPUT = 50_000
GLOB_MAX_MATCHES = 200
GREP_MAX_MATCHES = 500
GREP_MAX_FILE_BYTES = 2_000_000
GREP_LINE_LIMIT = 1000


class RequestError(Exception):
    def __init__(self, code, message, *, field=None, action="report"):
        super().__init__(message)
        self.code = code
        self.field = field
        self.action = action


def failure(code, message, *, action="report", field=None, unknown=False):
    return {
        "text": "Error: " + message,
        "ok": False,
        "error_code": code,
        "field": field,
        "action": "inspect_state" if unknown else action,
        "execution_status": "unknown" if unknown else "not_executed",
    }


def checked_path(root, raw):
    if not isinstance(raw, str) or not raw.strip():
        raise RequestError("INVALID_ARGUMENT", "path is required",
                           field="path", action="correct_arguments")
    target = (root / Path(raw).expanduser()).resolve()
    if not target.is_relative_to(root):
        raise RequestError("PERMISSION_DENIED", "path escapes workspace: " + raw)
    return target


def checked_pattern(root, pattern):
    """Reject escapes before glob starts traversing directories."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise RequestError("INVALID_ARGUMENT", "pattern is required",
                           field="pattern", action="correct_arguments")
    path = Path(pattern)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            raise RequestError("PERMISSION_DENIED", "pattern escapes workspace")
    if ".." in path.parts:
        raise RequestError("PERMISSION_DENIED", "parent traversal is not allowed in search patterns")
    return path.as_posix()


def scoped_matches(root, pattern):
    pattern = checked_pattern(root, pattern)
    for match in globlib.iglob(pattern, root_dir=str(root), recursive=True):
        try:
            if (root / match).resolve().is_relative_to(root):
                yield match
        except (OSError, RuntimeError):
            continue


def edit_file(args, root):
    target = checked_path(root, args.get("path"))
    old_text, new_text = args.get("old_text"), args.get("new_text")
    if not isinstance(old_text, str) or not old_text:
        raise RequestError("INVALID_ARGUMENT", "old_text is required",
                           field="old_text", action="correct_arguments")
    if not isinstance(new_text, str):
        raise RequestError("INVALID_ARGUMENT", "new_text is required",
                           field="new_text", action="correct_arguments")
    if not target.is_file():
        return failure("NOT_FOUND", f"no such file: {args['path']}", action="inspect")
    content = target.read_text(encoding="utf-8")
    count = content.count(old_text)
    replace_all = bool(args.get("replace_all"))
    if count == 0:
        return failure("PRECONDITION_FAILED", "old_text not found; read_file first, "
                       "then use the actual unique text.", action="inspect")
    if count > 1 and not replace_all:
        return failure("PRECONDITION_FAILED", f"old_text appears {count} times; "
                       "add surrounding context to make it unique, or pass replace_all.", action="inspect")
    updated = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
    try:
        target.write_text(updated, encoding="utf-8")
    except PermissionError as exc:
        return failure("PERMISSION_DENIED", str(exc), unknown=True)
    except (OSError, UnicodeError) as exc:
        return failure("TOOL_ERROR", str(exc), unknown=True)
    replaced = count if replace_all else 1
    return {"text": f"Edited {args['path']} ({replaced} replacement{'s' if replaced != 1 else ''})"}


def find_files(args, root):
    ordered = sorted({match.replace("\\", "/")
                      for match in scoped_matches(root, args.get("pattern"))})
    shown = ordered[:GLOB_MAX_MATCHES]
    if len(ordered) > GLOB_MAX_MATCHES:
        shown.append(f"... ({len(ordered) - GLOB_MAX_MATCHES} more matches; narrow the pattern)")
    return {"text": "\n".join(shown) if shown else "(no matches)"}


def preview(line, match):
    if len(line) <= GREP_LINE_LIMIT:
        return line
    window = GREP_LINE_LIMIT - 6
    start = max(0, min(match.start() - 40, len(line) - window))
    end = start + window
    return ("..." if start else "") + line[start:end] + ("..." if end < len(line) else "")


def search_files(args, workspace):
    pattern = args.get("pattern", "")
    if not isinstance(pattern, str) or not pattern:
        raise RequestError("INVALID_ARGUMENT", "pattern is required",
                           field="pattern", action="correct_arguments")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise RequestError("INVALID_ARGUMENT", f"invalid regex: {exc}",
                           field="pattern", action="correct_arguments")
    root = checked_path(workspace, args["path"]) if args.get("path") else workspace
    if not root.exists():
        return failure("NOT_FOUND", f"no such path: {args.get('path')}", action="inspect")
    include = args.get("include")
    include = include if isinstance(include, str) and include.strip() else "**/*"
    files = [root] if root.is_file() else sorted(
        root / match for match in scoped_matches(root, include) if (root / match).is_file()
    )
    results, output_chars, truncated = [], 0, False
    note = (f"... (results limited to {GREP_MAX_MATCHES} matching lines or "
            f"{MAX_TOOL_OUTPUT} characters; narrow the pattern or search a specific file)")
    budget = MAX_TOOL_OUTPUT - len(note) - 1
    for file in files:
        if truncated:
            break
        try:
            # Re-check resolved containment immediately before reading a match.
            resolved = file.resolve()
            if not resolved.is_relative_to(workspace):
                continue
            if file.stat().st_size > GREP_MAX_FILE_BYTES:
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
        except (OSError, RuntimeError, ValueError):
            continue
        if "\x00" in text[:1024]:
            continue
        relative = resolved.relative_to(workspace).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            match = regex.search(line)
            if match is not None:
                row = f"{relative}:{number}:{preview(line, match)}"
                row_chars = len(row) + (1 if results else 0)
                if len(results) >= GREP_MAX_MATCHES or output_chars + row_chars > budget:
                    truncated = True
                    break
                results.append(row)
                output_chars += row_chars
    if not results and not truncated:
        return {"text": "(no matches)"}
    if truncated:
        results.append(note)
    return {"text": "\n".join(results)}


def execute(tool, args, root):
    """The CLI always supplies /workspace; tests may supply a temporary root."""
    try:
        handlers = {"edit_file": edit_file, "glob": find_files, "grep": search_files}
        if tool not in handlers:
            raise RequestError("UNKNOWN_TOOL", f"unknown sandbox tool: {tool}")
        return handlers[tool](args, root.resolve())
    except RequestError as exc:
        return failure(exc.code, str(exc), field=exc.field, action=exc.action)
    except FileNotFoundError as exc:
        return failure("NOT_FOUND", str(exc), action="inspect")
    except PermissionError as exc:
        return failure("PERMISSION_DENIED", str(exc))
    except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
        return failure("TOOL_ERROR", str(exc))


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(json.dumps(execute(sys.argv[1], request, Path("/workspace")), ensure_ascii=True))
