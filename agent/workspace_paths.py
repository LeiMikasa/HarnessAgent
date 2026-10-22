"""Translate the mounted workspace alias without granting access to a path."""

from pathlib import Path


def host_workspace_path(raw: str, workdir: Path, *, sandbox_enabled: bool,
                        execution_mode: str = "default") -> str:
    """Map /workspace paths for file tools sharing the Docker bind mount.

    Keep traversal components for the caller's canonical-path containment check.
    Explicit host requests use literal host paths. Glob patterns remain patterns.
    """
    if sandbox_enabled and execution_mode != "host":
        posix = raw.replace("\\", "/")
        if posix == "/workspace" or posix.startswith("/workspace/"):
            relative = posix[len("/workspace"):].lstrip("/")
            return str(Path(workdir) / (relative or "."))
    return raw
