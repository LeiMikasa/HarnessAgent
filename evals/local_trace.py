"""Local evaluation payloads, outside the model-accessible workspace.

Production RunTrace remains payload-free. This recorder receives only model
payloads/tool outcomes, never SDK clients, headers, or grader answer keys.
"""

from __future__ import annotations

import json
from pathlib import Path


class EvaluationTrace:
    def __init__(self, directory: Path, *, secrets=()):
        self.directory = directory
        self.secrets = tuple(value for value in secrets if value)
        self.errors: list[str] = []
        self.model_calls = 0
        self.tool_calls = 0

    def _text(self, path: Path, text: str, *, append=False):
        try:
            for secret in self.secrets:
                text = text.replace(secret, "[REDACTED]")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a" if append else "w", encoding="utf-8", errors="replace") as stream:
                stream.write(text + "\n")
        except (OSError, ValueError, TypeError) as exc:
            if len(self.errors) < 16:
                self.errors.append(f"{path.name}: {type(exc).__name__}")

    def _write(self, path: Path, data, *, append=False):
        try:
            text = json.dumps(data, ensure_ascii=False, default=str)
        except (ValueError, TypeError) as exc:
            if len(self.errors) < 16:
                self.errors.append(f"{path.name}: {type(exc).__name__}")
            return
        self._text(path, text, append=append)

    def _timeline(self, text):
        self._text(self.directory / "timeline.md", text, append=True)

    def request(self, kwargs) -> Path:
        self.model_calls += 1
        folder = self.directory / "model-calls" / f"call-{self.model_calls:03d}"
        # Serialize now, before run_loop mutates the shared messages list.
        self._write(folder / "request.json", {
            key: kwargs[key] for key in ("model", "system", "messages", "tools", "max_tokens") if key in kwargs
        })
        kind = "Agent" if kwargs.get("tools") else "摘要"
        self._timeline(f"\n## 第 {self.model_calls} 次模型调用（{kind}）\n\n请求：model-calls/{folder.name}/request.json\n")
        return folder

    def response(self, folder: Path, response):
        self._write(folder / "response.json", {
            "content": response.content, "stop_reason": response.stop_reason,
            "usage": response.usage,
        })
        visible = []
        for block in response.content:
            if block.get("type") == "text":
                visible.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                visible.append("工具请求：" + str(block.get("name")) + " " +
                               json.dumps(block.get("input", {}), ensure_ascii=False))
        preview = "\n".join(visible)[:3000]
        self._timeline(f"\n停止标记：{response.stop_reason}\n\n```text\n{preview}\n```\n\n完整响应：model-calls/{folder.name}/response.json\n")

    def error(self, folder: Path, exc: BaseException):
        self._write(folder / "error.json", {"error_type": type(exc).__name__, "message": str(exc)})
        self._timeline(f"\n调用异常：{type(exc).__name__}\n\n```text\n{str(exc)[:2000]}\n```\n")

    def tool_result(self, block, result):
        self.tool_calls += 1
        self._write(self.directory / "tools.jsonl", {
            "sequence": self.tool_calls, "tool_use_id": block.get("id"),
            "tool": block.get("name"), "arguments": block.get("input", {}),
            "output": str(result), "ok": result.ok, "error_code": result.error_code,
            "execution_status": result.execution_status,
        }, append=True)
        args = json.dumps(block.get("input", {}), ensure_ascii=False, default=str)[:2000]
        self._timeline(f"\n### 工具 {self.tool_calls}：{block.get('name')}\n\n"
                       f"成功：{result.ok}；错误码：{result.error_code}\n\n"
                       f"```json\n{args}\n```\n\n```text\n{str(result)[:2000]}\n```\n\n"
                       f"完整工具结果：tools.jsonl 第 {self.tool_calls} 条。\n")
