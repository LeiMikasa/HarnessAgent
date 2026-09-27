"""Trusted acceptance checks, deliberately outside the agent's workspace."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Cannot import {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(workspace: Path) -> None:
    numbers = load_module(workspace / "numbers.py", "eval_numbers")
    strings = load_module(workspace / "strings.py", "eval_strings")

    values = [9, 1, 5, 3]
    assert numbers.median(values) == 4, "median: even-length input"
    assert values == [9, 1, 5, 3], "median must not mutate input"
    assert numbers.median([8, 2, 3]) == 3, "median: odd-length input"
    assert numbers.median([-4, 0]) == -2, "median: negative values"
    try:
        numbers.median([])
    except ValueError:
        pass
    else:
        raise AssertionError("median([]) must raise ValueError")

    samples = [1, 2, 3, 4]
    actual = numbers.moving_average(samples, 2)
    assert len(actual) == 3 and all(
        math.isclose(a, b) for a, b in zip(actual, [1.5, 2.5, 3.5])
    ), "moving_average: rolling windows"
    assert samples == [1, 2, 3, 4], "moving_average must not mutate input"
    assert numbers.moving_average([2, 6], 1) == [2, 6], "window=1"
    assert numbers.moving_average([2, 6], 3) == [], "oversized window"
    for invalid in (0, -1):
        try:
            numbers.moving_average([1, 2], invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("nonpositive window must raise ValueError")

    assert strings.slugify("  Crème brûlée & Café!  ") == "creme-brulee-cafe"
    assert strings.slugify("A__B---C") == "a-b-c"
    assert strings.slugify("你好") == ""
    assert strings.slugify("42 ways") == "42-ways"
    assert strings.word_frequencies("Hi, hi! Agent42 agent42.") == {
        "hi": 2, "agent42": 2
    }
    assert strings.word_frequencies("a_b a-b") == {"a": 2, "b": 2}
    assert strings.word_frequencies("!!!") == {}


if __name__ == "__main__":
    try:
        check(Path(sys.argv[1]).resolve())
    except Exception as exc:  # noqa: BLE001 - show a short actionable failure
        print(f"FAIL: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
    print("PASS")
