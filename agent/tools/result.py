"""Typed tool outcomes with backwards-compatible text for Python callers."""

from __future__ import annotations

import errno
import json


class ToolError(Exception):
    def __init__(self, code: str, message: str, *, field: str | None = None,
                 action: str = "report", transient: bool = False,
                 execution_status: str = "not_executed"):
        super().__init__(message)
        self.code = code
        self.field = field
        self.action = action
        self.transient = transient
        self.execution_status = execution_status


class ToolResult(str):
    """Keep existing string consumers working; never infer errors from text."""

    def __new__(cls, text: str, *, ok: bool = True, error_code: str | None = None,
                field: str | None = None, action: str = "none",
                transient: bool = False, execution_status: str = "succeeded",
                inspected_path: str | None = None):
        obj = super().__new__(cls, text)
        obj.ok = ok                       # 工具是否成功
        obj.error_code = error_code       # 错误类别
        obj.field = field                 # 哪个参数出错
        obj.action = action               # 建议接下来做什么
        obj.transient = transient         # 是否属于临时故障
        obj.execution_status = execution_status  # 操作执行到了什么状态
        obj.inspected_path = inspected_path  # 供内部状态恢复使用
        return obj

    @classmethod
    def failure(cls, code: str, message: str, *, field: str | None = None,
                action: str = "report", transient: bool = False,
                execution_status: str = "not_executed",
                inspected_path: str | None = None) -> "ToolResult":
        return cls(message, ok=False, error_code=code, field=field,
                   action=action, transient=transient, execution_status=execution_status,
                   inspected_path=inspected_path)

    @classmethod
    def from_exception(cls, exc: Exception, *, read_only: bool = False) -> "ToolResult":
        if isinstance(exc, ToolError):
            return cls.failure(exc.code, f"Error: {exc}", field=exc.field,
                               action=exc.action, transient=exc.transient,
                               execution_status=exc.execution_status)
        message = f"Error: {type(exc).__name__}: {exc}"
        status = "failed" if read_only else "unknown"
        if isinstance(exc, PermissionError):
            return cls.failure("PERMISSION_DENIED", message, execution_status=status)
        if isinstance(exc, FileNotFoundError):
            return cls.failure("NOT_FOUND", message, action="inspect", execution_status=status)
        transient = isinstance(exc, (TimeoutError, ConnectionError, InterruptedError)) or (
            isinstance(exc, OSError) and exc.errno in {
                errno.EAGAIN, errno.EINTR, errno.EBUSY, errno.ECONNRESET,
                errno.ECONNABORTED, errno.ETIMEDOUT,
            }
        )
        if transient:
            code = "TIMEOUT" if isinstance(exc, TimeoutError) else "TRANSIENT_ERROR"
            return cls.failure(code, message, transient=True,
                               action="report" if read_only else "inspect_state",
                               execution_status=status)
        return cls.failure("TOOL_ERROR", message,
                           action="report" if read_only else "inspect_state",
                           execution_status=status)

    def model_content(self) -> str:
        if self.ok:
            return str(self)
        return json.dumps({"ok": False, "error_code": self.error_code,
                           "message": str(self), "field": self.field,
                           "auto_retry": False, "next_action": self.action,
                           "recovery_hint": {
                               "correct_arguments": "Correct the reported arguments before calling again; do not guess missing content.",
                               "inspect": "Inspect the actual file, path or task state before issuing a corrected operation.",
                               "inspect_state": "The operation may have produced side effects. Inspect actual state; do not blindly repeat it.",
                               "report": "Report the failure or choose an appropriate alternative; do not bypass permissions.",
                           }.get(self.action, "Report the failure."),
                           "execution_status": self.execution_status}, ensure_ascii=False)


def tool_error(code: str, message: str, **kwargs) -> ToolResult:
    return ToolResult.failure(code, "Error: " + message, **kwargs)
