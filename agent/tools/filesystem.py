"""Session observations and guarded, atomic publication for the file tools.

Python implementation of the read-before-mutation design used by DSH.
Observations are not persisted. Locks coordinate this process's writers;
external writers are detected by version checks, not serialized by them.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import threading
import uuid
from weakref import WeakValueDictionary

from .result import ToolError


@dataclass(frozen=True)
class FileVersion:
    metadata: tuple[int, ...]
    digest: bytes


@dataclass(frozen=True)
class Snapshot:
    data: bytes
    version: FileVersion
    mode: int


class _TargetLock:
    def __init__(self):
        self.lock = threading.RLock()


_locks: WeakValueDictionary[str, _TargetLock] = WeakValueDictionary()
_locks_guard = threading.Lock()
_UNSEEN = object()


def _key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


@contextmanager
def _locked(path: Path):
    key = _key(path)
    with _locks_guard:
        holder = _locks.get(key)
        if holder is None:
            holder = _TargetLock()
            _locks[key] = holder
    with holder.lock:
        yield


def _metadata(info: os.stat_result) -> tuple[int, ...]:
    # Windows path stat and handle fstat may expose creation/change time with
    # different ctime semantics (notably after hard-link publication). Content
    # hashing, file identity, size, mtime and mode still detect stale content.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns if os.name != "nt" else 0, info.st_mode)


def _stale(path: Path, *, status: str = "not_executed") -> ToolError:
    return ToolError("FS_STALE_VERSION", f'File changed since it was observed: "{path}"; '
                     "read_file again, inspect the current content, then retry.",
                     action="reread", execution_status=status)


def _snapshot(path: Path) -> Snapshot | None:
    try:
        initial = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(initial.st_mode):
        raise ToolError("PRECONDITION_FAILED", f'"{path}" is not a regular file; use glob for directories.',
                        action="inspect")
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            data = stream.read()
            after = os.fstat(stream.fileno())
        current = path.stat()
    except FileNotFoundError as exc:
        raise _stale(path) from exc
    if not (_metadata(initial) == _metadata(before) == _metadata(after) == _metadata(current)):
        raise _stale(path)
    version = FileVersion(_metadata(current), hashlib.sha256(data).digest())
    return Snapshot(data, version, stat.S_IMODE(current.st_mode))


def _replace_existing(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        return
    # ReplaceFileW preserves the target's Windows DACL. os.replace would give
    # the target the staging file's ACL. Fail rather than silently downgrade.
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    replace = kernel.ReplaceFileW
    replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    replace.restype = wintypes.BOOL
    if not replace(str(target), str(source), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


class ObservedFiles:
    """One caller/session's observed versions; no cached file contents."""

    def __init__(self):
        self._observed: dict[str, FileVersion | None] = {}
        self._guard = threading.RLock()

    def clear(self) -> None:
        with self._guard:
            self._observed.clear()

    def _get(self, path: Path):
        with self._guard:
            return self._observed.get(_key(path), _UNSEEN)

    def _record(self, path: Path, version: FileVersion | None) -> None:
        with self._guard:
            self._observed[_key(path)] = version

    def _forget(self, path: Path) -> None:
        with self._guard:
            self._observed.pop(_key(path), None)

    def read(self, path: Path) -> str:
        with _locked(path):
            snapshot = _snapshot(path)
            if snapshot is None:
                self._record(path, None)
                raise FileNotFoundError(f'no such file: {path}')
            text = _normalize(snapshot.data.decode("utf-8", errors="replace"))
            self._record(path, snapshot.version)
            return text

    def _check(self, path: Path, current: Snapshot | None, expected, *, editing=False) -> None:
        if editing and expected is _UNSEEN:
            raise ToolError("FS_NOT_OBSERVED", f'Cannot modify "{path}": file has not been read; '
                            "use read_file first, then retry.", action="reread")
        if editing and expected is None:
            raise ToolError("FS_NOT_FOUND", f'Cannot edit "{path}": it was observed absent; '
                            "use write_file to create it, or read_file again.", action="reread")
        if expected is _UNSEEN:
            if current is not None:
                raise ToolError("FS_NOT_OBSERVED", f'Cannot overwrite "{path}": file has not been read; '
                                "use read_file first, then retry.", action="reread")
        elif (current.version if current else None) != expected:
            raise _stale(path)

    def _publish(self, path: Path, data: bytes, current: Snapshot | None, expected) -> None:
        temporary = None
        publication_attempted = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            candidate = path.parent / f".minicode-{uuid.uuid4().hex}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            # New files retain ordinary open(..., 'w') umask semantics. Existing
            # content stays private while staging, then inherits its old mode.
            fd = os.open(candidate, flags, 0o600 if current is not None else 0o666)
            temporary = candidate
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if current is not None:
                os.chmod(temporary, current.mode)
            # A second check catches external changes during staging. Writers
            # using this module are locked throughout check -> stage -> commit.
            self._check(path, _snapshot(path), expected)
            publication_attempted = True
            if current is None:
                try:
                    # Unlike replace(), this cannot overwrite a concurrent creator.
                    os.link(temporary, path)
                except FileExistsError as exc:
                    raise _stale(path) from exc
            else:
                _replace_existing(temporary, path)
        except ToolError:
            raise
        except OSError as exc:
            if publication_attempted:
                self._forget(path)
            raise ToolError("FS_IO_ERROR", f'Cannot publish "{path}": {exc}',
                            action="inspect_state" if publication_attempted else "report",
                            execution_status="unknown" if publication_attempted else "not_executed") from exc
        finally:
            if temporary is not None:
                # Only this operation's exclusive temporary file is removed.
                try:
                    temporary.unlink(missing_ok=True)
                except PermissionError:
                    # A failed Windows replacement of a read-only target may
                    # leave our equally read-only staging file behind.
                    try:
                        temporary.chmod(stat.S_IREAD | stat.S_IWRITE)
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                except OSError:
                    pass
        # Hard-link creation and staging cleanup change ctime, so observe only
        # after cleanup. Never bless content an external writer added afterward.
        try:
            committed = _snapshot(path)
            if committed is None or committed.data != data:
                raise _stale(path, status="unknown")
        except (OSError, ToolError) as exc:
            self._forget(path)
            raise ToolError("FS_STALE_VERSION", f'Cannot verify committed file "{path}": {exc}; '
                            "read_file to inspect actual state before retrying.",
                            action="reread", execution_status="unknown") from exc
        self._record(path, committed.version)

    def write(self, path: Path, content: str) -> None:
        with _locked(path):
            expected = self._get(path)
            current = _snapshot(path)
            self._check(path, current, expected)
            self._publish(path, content.encode("utf-8"), current, expected)

    def edit(self, path: Path, old_text: str, new_text: str, *, replace_all=False) -> int:
        with _locked(path):
            expected = self._get(path)
            current = _snapshot(path)
            self._check(path, current, expected, editing=True)
            content = _normalize(current.data.decode("utf-8"))
            old_text, new_text = _normalize(old_text), _normalize(new_text)
            count = content.count(old_text)
            if count == 0 or (count > 1 and not replace_all):
                detail = "old_text not found" if count == 0 else f"old_text appears {count} times"
                raise ToolError("PRECONDITION_FAILED", f'{detail} in "{path}"; read_file and use '
                                "actual unique text, or pass replace_all for multiple matches.", action="inspect")
            updated = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
            raw = current.data.decode("utf-8")
            crlf = raw.count("\r\n")
            if crlf > raw.count("\n") - crlf:
                updated = updated.replace("\n", "\r\n")
            self._publish(path, updated.encode("utf-8"), current, expected)
            return count if replace_all else 1
