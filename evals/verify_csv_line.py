"""Trusted acceptance checks for the verification-feedback evaluation.

This file is outside the agent workspace. Keep failures short and diagnostic:
the experiment compares generic failure notice with this exact detail.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path


def load_parser(workspace: Path):
    path = workspace / "csv_line.py"
    spec = importlib.util.spec_from_file_location("eval_csv_line", path)
    if spec is None or spec.loader is None:
        raise AssertionError("csv_line.py cannot be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_csv_line


def check(workspace: Path) -> None:
    source = (workspace / "csv_line.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(
            alias.name == "csv" or alias.name.startswith("csv.") for alias in node.names
        ):
            raise AssertionError("csv module imports are not allowed for this task")
        if isinstance(node, ast.ImportFrom) and (
            node.module == "csv" or (node.module or "").startswith("csv.")
        ):
            raise AssertionError("csv module imports are not allowed for this task")
    parse = load_parser(workspace)
    examples = [
        ("plain fields", "a,b,c", ["a", "b", "c"]),
        ("empty record", "", [""]),
        ("trailing empty field", "a,", ["a", ""]),
        ("empty fields", ",,", ["", "", ""]),
        ("quoted comma", '"a,b",c', ["a,b", "c"]),
        ("escaped quote", '"a""b",c', ['a"b', "c"]),
        ("quoted line break", '"a\nb",c', ["a\nb", "c"]),
        ("preserved whitespace", ' a ," b "', [" a ", " b "]),
        ("quoted empty field", '"",x', ["", "x"]),
    ]
    for name, line, expected in examples:
        try:
            actual = parse(line)
        except Exception as exc:  # noqa: BLE001 - convert to a concise case failure
            raise AssertionError(
                f"{name}: input={line!r}, expected={expected!r}, raised {type(exc).__name__}: {exc}"
            ) from exc
        if actual != expected:
            raise AssertionError(
                f"{name}: input={line!r}, expected={expected!r}, got={actual!r}"
            )
    for name, line in [
        ("unclosed quote", '"abc'),
        ("quote inside unquoted field", 'ab"cd'),
        ("text after closing quote", '"ab"x'),
        ("quote in second unquoted field", 'a,b"c'),
    ]:
        try:
            parse(line)
        except ValueError:
            continue
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"{name}: input={line!r}, expected ValueError, raised {type(exc).__name__}"
            ) from exc
        raise AssertionError(f"{name}: input={line!r}, expected ValueError, returned normally")


if __name__ == "__main__":
    try:
        check(Path(sys.argv[1]).resolve())
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
    print("PASS")
