from pathlib import Path
import subprocess
import uuid
import json
import os
from pathlib import PurePosixPath
from .tools.result import ToolResult,tool_error
from textwrap import dedent

class DockerSandbox:
    def __init__(self,workspace:str):
        self.workspace=Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"工作目录不存在: {self.workspace}")
        self.name=f"harness-{uuid.uuid4().hex[:12]}"
        self.started=False

    def _docker(self,args: list[str],timeout=60,input_text=None):
        """在宿主机调用 Docker 客户端"""
        return subprocess.run(
            ["docker",*args],
            capture_output=True,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    def start(self):
        """启动一个后台容器"""
        if self.started:
            return
        result=self._docker([
            "run",
            "--detach",
            "--name",self.name,
            "--init",
            "--network", "bridge",
            "--memory", "2g",
            "--cpus", "2",
            "--pids-limit", "128",
            "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges=true",
            "--user", "node",
            "--mount",
            f"type=bind,source={self.workspace},target=/workspace",
            "--workdir", "/workspace",
            "harness-sandbox:dev",
            "sleep", "infinity",
        ])

        if result.returncode != 0:
            raise RuntimeError(f"容器启动失败: {result.stderr}")

        self.started=True
        print("本次创建的容器名称：", self.name)
        print("本次创建的容器 ID：", result.stdout.strip())
    def execute(self,command: str,timeout=120):
        """在已有的容器内执行命令，保留退出码与输出"""
        if not self.started:
            raise RuntimeError("请先调用 start()")
        try:
            return self._docker([
                "exec",
                "--workdir", "/workspace",
                self.name,
                "bash", "-c", command
            ],timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # 结束Docker客户端的等待，不保证容器里的命令停止
            # 这里关闭整个任务容器，终止其中的进程
            self.close()
            raise TimeoutError(
                "命令超时，任务容器已关闭；已完成的文件修改仍保留"
            ) from exc
    def read_file(self,args:dict) -> ToolResult:
        if not self.started:
            raise RuntimeError("请先启动沙箱")
        # 将宿主机工作区内的绝对路径转换为相对路径
        request=dict(args)
        raw=request["path"]

        if Path(raw).is_absolute() and not raw.startswith("/"):
            try:
                raw=Path(raw).relative_to(self.workspace).as_posix()
            except ValueError:
                return tool_error(
                    "PERMISSION_DENIED",
                    "文件不在沙箱工作区内",
                )
        request["path"]=raw.replace("\\","/")
        # 这段固定脚本交给容器里的python执行
        script=r'''
import json
import sys
from pathlib import Path

def failure(code, message, action="report", inspected=None):
    return {
        "text": message,
        "ok": False,
        "error_code": code,
        "action": action,
        "execution_status": "failed",
        "inspected_path": inspected,
    }

def read(args):
    root = Path("/workspace").resolve()
    target = (root / args["path"]).resolve()

    if not target.is_relative_to(root):
        return failure("PERMISSION_DENIED", "不允许读取工作区外的文件")

    try:
        if target.is_dir():
            return failure(
                "PRECONDITION_FAILED",
                "目标是目录，请使用 glob",
                "inspect",
            )

        text = target.read_text(encoding="utf-8", errors="replace")

    except FileNotFoundError:
        return failure(
            "NOT_FOUND",
            "文件不存在",
            "inspect",
            str(target),
        )
    except PermissionError as exc:
        return failure("PERMISSION_DENIED", str(exc))
    except OSError as exc:
        return failure("TOOL_ERROR", str(exc))

    lines = text.splitlines()
    start = max(0, (args.get("offset") or 1) - 1)
    limit = args.get("limit")
    end = len(lines) if not limit else min(len(lines), start + limit)

    body = "\n".join(lines[start:end])

    notes = []
    if start > 0:
        notes.append(f"started at line {start + 1}")
    if end < len(lines):
        notes.append(f"{len(lines) - end} more lines")
    if notes:
        body += "\n... (" + "; ".join(notes) + ")"

    complete = start == 0 and end == len(lines) and len(body) <= 50000

    output = body[:50000]
    if len(body) > 50000:
        output += f"\n... (truncated, {len(body) - 50000} more characters)"

    return {
        "text": output or "(empty file)",
        "inspected_path": str(target) if complete else None,
    }

print(json.dumps(read(json.loads(sys.argv[1])), ensure_ascii=True))
        '''
        # 启动容器里的python，取得它返回的JSON
        try:
            result=self._docker([
                "exec",
                self.name,
                "/usr/bin/python3",
                "-I",
                "-c",script,
                json.dumps(request,ensure_ascii=True)
            ],timeout=30)
        except subprocess.TimeoutExpired as exc:
            self.close()
            raise TimeoutError("读取超时，容器已关闭") from exc

        if result.returncode!=0:
            raise RuntimeError(result.stderr)
        data=json.loads(result.stdout)
        # 将完整读取标记转换回宿主机路径，供ToolRecovery使用
        inspected=data.get("inspected_path")
        if inspected is not None:
            relative = PurePosixPath(inspected).relative_to("/workspace")
            data["inspected_path"] = os.path.normcase(
                str(self.workspace.joinpath(*relative.parts))
            )

        return ToolResult(**data)
    def write_file(self,args: dict) -> ToolResult:
        if not self.started:
            raise RuntimeError("请先启动沙箱")
        request=dict(args)
        if not isinstance(request.get("content"),str):
            return tool_error(
                "INVALID_ARGUMENT",
                "content 必须是字符串",
                field="content",
                action="correct_arguments"
            )
        # 和读取工具一样，将工作区内的windows绝对路径转为相对路径
        raw=request["path"]

        if Path(raw).is_absolute() and not raw.startswith("/"):
            try:
                raw=Path(raw).relative_to(self.workspace).as_posix()
            except ValueError:
                return tool_error(
                    "PERMISSION_DENIED",
                    "文件不在沙箱工作区内",
                )
        request["path"]=raw.replace("\\","/")
        script = r'''
        import json
        import sys
        from pathlib import Path

        def failure(code, message, action="report", unknown=False):
            return {
                "text": message,
                "ok": False,
                "error_code": code,
                "action": "inspect_state" if unknown else action,
                "execution_status": "unknown" if unknown else "not_executed",
            }

        def write(args):
            root = Path("/workspace").resolve()
            target = (root / args["path"]).resolve()

            if not target.is_relative_to(root):
                return failure(
                    "PERMISSION_DENIED",
                    "不允许写入工作区外的文件",
                )

            if target.is_dir():
                return failure(
                    "PRECONDITION_FAILED",
                    "目标是目录，请指定文件路径",
                    action="inspect",
                )

            content = args["content"]

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            except PermissionError as exc:
                return failure(
                    "PERMISSION_DENIED",
                    str(exc),
                    unknown=True,
                )
            except OSError as exc:
                return failure(
                    "TOOL_ERROR",
                    str(exc),
                    unknown=True,
                )

            return {
                "text": f"Wrote {len(content.encode('utf-8'))} bytes to {args['path']}",
            }

        request = json.load(sys.stdin)
        print(json.dumps(write(request), ensure_ascii=True))
        '''
        script = dedent(script)
        try:
            result=self._docker([
                "exec",
                "-i",
                self.name,
                "/usr/bin/python3",
                "-I",
                "-c",script
            ],
            timeout=30,
            input_text=json.dumps(request,ensure_ascii=True)
            ,)
        except subprocess.TimeoutExpired as exc:
            self.close()
            raise TimeoutError(
                "写入超时，容器已经关闭；请检查实际文件状态"
            ) from exc
        if result.returncode!=0:
            raise RuntimeError(result.stderr)

        return ToolResult(**json.loads(result.stdout))
    def _run_file_tool(self, name: str, args: dict) -> ToolResult:
        """Send trusted worker code and JSON input to the existing container."""
        if not self.started:
            raise RuntimeError("请先启动沙箱")

        request = dict(args)
        # Only path-like fields are normalized; grep's pattern is a regex.
        fields = ("pattern",) if name == "glob" else ("path", "include")
        for field in fields:
            raw = request.get(field)
            if not isinstance(raw, str):
                continue
            if Path(raw).is_absolute() and not raw.startswith("/"):
                try:
                    raw = Path(raw).relative_to(self.workspace).as_posix()
                except ValueError:
                    return tool_error("PERMISSION_DENIED", "文件不在沙箱工作区内")
            request[field] = raw.replace("\\", "/")

        script = Path(__file__).with_name("sandbox_file_tools.py").read_text(encoding="utf-8")
        try:
            result = self._docker(
                ["exec", "-i", self.name, "/usr/bin/python3", "-I", "-c", script, name],
                timeout=30,
                input_text=json.dumps(request, ensure_ascii=True),
            )
        except subprocess.TimeoutExpired as exc:
            self.close()
            raise TimeoutError(f"{name} 超时，容器已关闭；请检查实际文件状态") from exc

        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        return ToolResult(**json.loads(result.stdout))

    def edit_file(self, args: dict) -> ToolResult:
        return self._run_file_tool("edit_file", args)

    def glob(self, args: dict) -> ToolResult:
        return self._run_file_tool("glob", args)

    def grep(self, args: dict) -> ToolResult:
        return self._run_file_tool("grep", args)

    def close(self):
        """关闭并删除对象创建的容器"""
        if not self.started:
            return

        result=self._docker(["rm", "--force", self.name])

        if result.returncode!=0:
            raise RuntimeError(
                f"容器清理失败，请检查{self.name}: {result.stderr}"
            )
        self.started=False
