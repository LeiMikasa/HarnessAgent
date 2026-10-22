"""Host file tools plus Docker Bash share files and enforce boundaries; no model calls."""

from pathlib import Path
from tempfile import TemporaryDirectory

from agent.runtime import Runtime


def main():
    scratch = Path(__file__).resolve().parent / ".scratch"
    scratch.mkdir(exist_ok=True)
    # A new workspace on every run; existing practice files are left alone.
    with TemporaryDirectory(prefix="sandbox-files-", dir=scratch) as directory:
        workspace = Path(directory) / "project"
        workspace.mkdir()
        outside = Path(directory) / "outside.txt"
        outside.write_text("private-outside", encoding="utf-8")
        runtime = Runtime.create(
            provider="mock", workdir=workspace, permission_mode="workspace",
            teams_enabled=False, memory_enabled=False, approval="deny",
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

            result = call("grep", pattern="8081", path=str(workspace / path))
            assert result.ok and "8081" in result, result
            assert call("read_file", path="/workspace/" + path) == "端口=8081\nname=B\nname=B"
            assert runtime.sandbox is None, "File tools unexpectedly started Docker"
            print("PASS: host absolute path and /workspace alias, no Docker for files")

            # Direct registry calls also contain paths without permission hooks.
            for name, args in (
                ("read_file", {"path": str(outside)}),
                ("edit_file", {"path": "/workspace/../outside.txt", "old_text": "private", "new_text": "changed"}),
                ("glob", {"pattern": "../*"}),
                ("grep", {"path": str(outside), "pattern": "private"}),
            ):
                result = runtime.tools.dispatch(name, args, runtime.lead_ctx)
                assert result.error_code == "PERMISSION_DENIED", result
            assert outside.read_text(encoding="utf-8") == "private-outside"
            print("PASS: host absolute/parent/alias escapes blocked without hooks")

            assert call("write_file", path="many.txt", content="needle\n" * 501).ok
            result = call("grep", pattern="needle", path="many.txt")
            assert result.ok and len(result.splitlines()) == 501 and "results limited" in result, result
            print("PASS: grep 500-match limit")
            result = call("bash", command="printf from-container > src/nested/shared.txt")
            assert result.ok, result
            assert call("read_file", path="/workspace/src/nested/shared.txt") == "from-container"
            assert call("write_file", path="src/nested/shared.txt", content="from-host").ok
            result = call("bash", command='python3 -c "from pathlib import Path; assert Path(\'src/nested/shared.txt\').read_text() == \'from-host\'; print(\'shared-ok\')"')
            assert result.ok and "shared-ok" in result, result
            print("PASS: Docker Bash and host file tools see the same bind-mounted files")
            print("All hybrid file-tool checks passed.")
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
