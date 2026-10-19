"""The shell actually used by subprocess, and decoding for its captured pipes."""

from __future__ import annotations

import locale
import os
import sys


def shell_name() -> str:
    if sys.platform == "win32":
        return os.environ.get("COMSPEC") or "cmd.exe"
    return "/bin/sh"


def shell_guidance(*,sandbox_enabled: bool=False) -> str:
    if sandbox_enabled:
          return (
            "The bash tool executes commands in a Linux Docker container "
            "through /bin/bash. Its working directory is /workspace, "
            "which is the mounted host project directory. "
            "Use Linux Bash syntax and container paths in shell commands. "
            "Python is available as python3. "
            "Network access is disabled. "
            "Each call starts a new shell in /workspace; "
            "cd and exported variables do not persist across calls. "
            "Other file tools currently operate on the host workspace: "
            "pass workspace-relative paths to those tools, "
            "not container-absolute /workspace paths."
        )
    platform = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, sys.platform)
    description = f"Operating system: {platform}. The bash tool runs commands through {shell_name()}. "
    if sys.platform == "win32":
        description += (
            "Use cmd syntax, not PowerShell or POSIX shell syntax; the tool name does not imply Bash. "
            "Unix utilities such as tail are not guaranteed to exist. Use glob/grep/read_file for "
            "file inspection, or invoke a specific installed interpreter explicitly. "
        )
    return description + (
        "For complex JavaScript or Python diagnostics, write a short script in the workspace and "
        "run it instead of nesting multiple quoting layers in node -e or python -c."
    )



def decode_shell_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if value.startswith((b"\xff\xfe", b"\xfe\xff")):
        return value.decode("utf-16", errors="replace")
    # Node and many modern CLIs emit UTF-8 even on a non-UTF-8 Windows locale.
    encodings = ["utf-8-sig"]
    if sys.platform == "win32":
        encodings.extend(("oem", "mbcs"))
    encodings.append(locale.getencoding() if hasattr(locale, "getencoding")
                     else locale.getpreferredencoding(False))
    for encoding in dict.fromkeys(encodings):
        try:
            return value.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return value.decode("utf-8", errors="replace")
