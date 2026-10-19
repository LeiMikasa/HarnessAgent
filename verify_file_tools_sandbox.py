"""Real Docker smoke check for edit_file/glob/grep; no paid model calls."""

from pathlib import Path
from tempfile import TemporaryDirectory

from agent.runtime import Runtime


def main():
    scratch = Path(__file__).resolve().parent / ".scratch"
    scratch.mkdir(exist_ok=True)
    # A new workspace on every run; existing practice files are left alone.
    with TemporaryDirectory(prefix="sandbox-files-", dir=scratch) as directory:
        runtime = Runtime.create(
            provider="mock", workdir=directory, sandbox_enabled=True,
            teams_enabled=False, memory_enabled=False, approval="allow",
        )
        number = 0

        def call(name, **args):
            nonlocal number
            number += 1
            return runtime.execute_tool({
                "type": "tool_use", "id": f"file-check-{number}",
                "name": name, "input": args,
            })

        try:
            path = "src/nested/config.txt"
            original = "端口=8080\nname=A\nname=A"
            assert call("write_file", path=path, content=original).ok
            result = call("edit_file", path=path, old_text="8080", new_text="8081")
            assert result.ok, result
            print("PASS: edit_file unique replacement")

            for old in ("not-present", "name=A"):
                result = call("edit_file", path=path, old_text=old, new_text="wrong")
                assert result.error_code == "PRECONDITION_FAILED", result
                result = call("read_file", path=path)
                assert result.ok and str(result) == "端口=8081\nname=A\nname=A", result
            result = call("edit_file", path=path, old_text="name=A", new_text="name=B", replace_all=True)
            assert result.ok, result
            result = call("read_file", path=path)
            assert result.ok and str(result) == "端口=8081\nname=B\nname=B", result
            print("PASS: ambiguous/missing edits rejected; replace_all succeeds")

            result = call("glob", pattern="src/**/*.txt")
            assert result.ok and str(result) == path, result
            result = call("glob", pattern="src/**/*.missing")
            assert result.ok and str(result) == "(no matches)", result
            print("PASS: glob nested files and no matches")

            result = call("grep", pattern=r"端口=\d+", path="src", include="**/*.txt")
            assert result.ok and str(result) == f"{path}:1:端口=8081", result
            result = call("grep", pattern="[bad", path=path)
            assert result.error_code == "INVALID_ARGUMENT", result
            print("PASS: grep regex, include, line numbers, invalid regex")

            # Windows absolute workspace paths are translated for the container.
            result = call("grep", pattern="8081", path=str(Path(directory) / path))
            assert result.ok and "8081" in result, result
            print("PASS: Windows absolute path")

            box = runtime.sandbox
            # Direct calls exercise container checks independently of permission hooks.
            for result in (
                box.edit_file({"path": "/etc/passwd", "old_text": "root", "new_text": "changed"}),
                box.glob({"pattern": "/etc/*"}),
                box.grep({"path": "/etc/passwd", "pattern": "root"}),
                box.glob({"pattern": "../*"}),
            ):
                assert result.error_code == "PERMISSION_DENIED", result

            # Symlink targets outside the workspace must not be returned or read.
            command = box.execute("ln -s /etc/passwd /workspace/outside-link")
            assert command.returncode == 0, command.stderr
            assert box.glob({"pattern": "outside-link"}) == "(no matches)"
            assert box.grep({"path": "outside-link", "pattern": "root"}).error_code == "PERMISSION_DENIED"
            assert box.edit_file({"path": "outside-link", "old_text": "root", "new_text": "changed"}).error_code == "PERMISSION_DENIED"
            print("PASS: absolute/parent/symlink escapes blocked")
            command = box.execute("unlink /workspace/outside-link")
            assert command.returncode == 0, command.stderr

            assert call("write_file", path="many.txt", content="needle\n" * 501).ok
            result = call("grep", pattern="needle", path="many.txt")
            assert result.ok and len(result.splitlines()) == 501 and "results limited" in result, result
            print("PASS: grep 500-match limit")
            print("All Docker file-tool checks passed.")
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
