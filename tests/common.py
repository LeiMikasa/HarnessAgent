"""Shared test helpers."""

from __future__ import annotations

import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import Settings  # noqa: E402
from agent.runtime import Runtime  # noqa: E402

#: Scratch space for tests.  Deliberately inside the project rather than the OS
#: temp directory, so the suite works unchanged under a write-confined sandbox.
SCRATCH_ROOT = Path(__file__).resolve().parent.parent / ".scratch" / "tests"


def _make_tmpdir() -> Path:
    """A fresh, unique, writable scratch directory.

    Deliberately not `tempfile.mkdtemp`: on Windows its 0o700 directories
    reject subsequent nested creation under a write-confined sandbox, which
    would make every case fail for reasons unrelated to the harness.
    """
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    for _ in range(64):
        candidate = SCRATCH_ROOT / f"case-{uuid.uuid4().hex[:10]}"
        try:
            candidate.mkdir(parents=True)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError("could not allocate a scratch directory")


def make_settings(workdir: Path, **overrides) -> Settings:
    base = dict(
        provider="mock",
        model="mock-1",
        api_key="mock",
        base_url=None,
        workdir=workdir.resolve(),
        state_dir=(workdir / ".agent").resolve(),
        skill_dirs=[(workdir / "skills").resolve()],
        max_tokens=8000,
        max_turns=20,
        subagent_max_turns=8,
    )
    base.update(overrides)
    return Settings(**base)


class HarnessCase(unittest.TestCase):
    """A test case with a throwaway workspace and a mock-model runtime."""

    def setUp(self) -> None:
        self.tmp = _make_tmpdir()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make_runtime(self, script=None, settings=None, memory: bool = False, **kwargs) -> Runtime:
        """Build a runtime for a test.

        Memory is off by default: extraction and selection make their own model
        calls, which would consume scripted mock turns and make every other
        test's turn accounting confusing.  Memory tests opt in explicitly.
        """
        settings = settings or make_settings(self.tmp)
        kwargs.setdefault("approval", "allow")
        kwargs.setdefault("memory_enabled", memory)
        runtime = Runtime.create(settings, script=script, **kwargs)
        self.addCleanup(runtime.close)
        return runtime

    def write(self, relative: str, content: str) -> Path:
        path = self.tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def make_skill(self, name: str, description: str, body: str = "Do the thing.") -> Path:
        path = self.tmp / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
            encoding="utf-8",
        )
        return path

    # -- assertions ------------------------------------------------------

    def tool_result_texts(self, runtime: Runtime) -> list[str]:
        """Every tool_result payload in the conversation, in order."""
        out: list[str] = []
        for message in runtime.messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out.append(str(block.get("content", "")))
        return out

    def called_tools(self, runtime: Runtime) -> list[str]:
        return [call["name"] for call in runtime.llm.tool_calls]
