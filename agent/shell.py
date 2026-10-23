"""The shell actually used by subprocess, and decoding for its captured pipes."""

from __future__ import annotations

import locale
import os
import subprocess
import sys


def noninteractive_process_options(*, input_provided: bool = False) -> dict:
    """Keep tool processes away from the TUI's input and Windows console.

    Capturing stdout/stderr alone does not detach a Windows child. cmd.exe can
    re-enable echo/line input on the shared console, exposing SGR mouse reports.
    Explicit input (such as Docker's JSON requests) still uses its own pipe.
    """
    options = {} if input_provided else {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


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
            "Bash pipefail is enabled: a failed pipeline stage is reported even if tail/head succeeds. "
            "For recognized tests/builds, run the check directly: output is saved automatically, "
            "and pipelines/fallbacks that could hide its exit status are rejected. "
            "A producer may report SIGPIPE when head stops reading; inspect that cause instead of "
            "treating every nonzero pipeline as a dependency problem. "
            "Python is available as python3. "
            "Network access uses Docker bridge networking. "
            "Each call starts a new shell in /workspace; "
            "cd and exported variables do not persist across calls. "
            "File tools execute on the host with application permission checks; "
            "prefer workspace-relative paths. /workspace paths map to the mounted host workspace "
            "for default file calls. File tools cannot access container-only files. Host files outside the workspace "
            "require one-call human approval. For a host command, set "
            "execution_mode='host' and justification; approval applies to one invocation "
            f"only. Host commands use {shell_name()}, with host paths and shell syntax, "
            "not container paths or Linux Bash syntax on Windows. "
            "A Docker error never automatically falls back to host execution."
        )
    platform = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, sys.platform)
    description = f"Operating system: {platform}. The bash tool runs commands through {shell_name()}. "
    if sys.platform == "win32":
        description += (
            "Use cmd syntax, not PowerShell or POSIX shell syntax; the tool name does not imply Bash. "
            "Unix utilities such as tail are not guaranteed to exist. File discovery, content search "
            "and text reading must use the dedicated file tools, not shell commands or scripts. "
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
