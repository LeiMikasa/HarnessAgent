"""Glob traversal that prunes dependency/build directories before entering them."""

from __future__ import annotations

import fnmatch
import glob
import os
import re
from pathlib import Path

IGNORED_DIRECTORIES = frozenset({
    "node_modules", ".venv", "venv", ".git", "__pycache__",
    "dist", "build", "coverage", ".next", ".nuxt", ".cache",
})


def has_brace_expansion(pattern: str) -> bool:
    # Braces used literally (including [{] and [}]) remain valid filenames.
    without_classes = re.sub(r"\[[^\]]*\]", "", pattern)
    return bool(re.search(r"\{[^{}]*(?:,|\.\.)[^{}]*\}", without_classes))


def glob_paths(root: Path, pattern: str, *, include_ignored: bool = False,
               allow_outside: bool = False):
    """Yield Paths relative to root (or absolute for an absolute pattern).

    Literal directory components opt into that directory. Recursive traversal
    keeps an ancestry set so symlink cycles terminate, and checks containment
    before scanning directories, not just before returning matches.
    """
    pattern_path = Path(pattern).expanduser()
    parts = pattern_path.parts
    base = Path(pattern_path.anchor) if pattern_path.is_absolute() else Path()
    if pattern_path.is_absolute():
        parts = parts[1:]
    explicit = {os.path.normcase(part) for part in parts if not glob.has_magic(part)}
    ignored = {os.path.normcase(name) for name in IGNORED_DIRECTORIES} - explicit
    directory_only = pattern.endswith(("/", "\\"))
    # Start at the literal prefix: absolute patterns must not scan drive roots.
    while parts and not glob.has_magic(parts[0]):
        base /= parts[0]
        parts = parts[1:]

    def visit(path, index, ancestors):
        target = root / path
        try:
            resolved = target.resolve()
            if not allow_outside and not resolved.is_relative_to(root):
                return
            if index == len(parts):
                if (path != Path() or pattern in (".", "./")) and target.exists() and (not directory_only or target.is_dir()):
                    yield path
                return
            part = parts[index]
            if part == "**":
                yield from visit(path, index + 1, ancestors)
                if not target.is_dir() or resolved in ancestors:
                    return
                ancestors = ancestors | {resolved}
            elif not target.is_dir():
                return
            # Literal components can name hidden or explicitly ignored paths.
            elif not glob.has_magic(part):
                yield from visit(path / part, index + 1, ancestors)
                return
            with os.scandir(target) as entries:
                for entry in entries:
                    if entry.name.startswith(".") and not (
                            include_ignored or part.startswith(".") or os.path.normcase(entry.name) in explicit):
                        continue
                    if not include_ignored and os.path.normcase(entry.name) in ignored and (
                            entry.is_dir() or entry.name == ".git"):
                        continue
                    if part == "**":
                        yield from visit(path / entry.name, index, ancestors)
                    elif fnmatch.fnmatch(entry.name, part):
                        yield from visit(path / entry.name, index + 1, ancestors)
        except (OSError, RuntimeError):
            # Unreadable paths and broken/cyclic symlinks are not search results.
            return

    yield from visit(base, 0, frozenset())
