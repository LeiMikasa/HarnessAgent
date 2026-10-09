"""Per-loop repair budget and conservative, host-controlled tool retries."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from pathlib import Path

from .result import ToolResult, tool_error
# 这是模型的修正调用次数
# 记录哪些错误要进入"模型修正次数"
#  参数不符合规则   工具名不存在   文件，任务等目标不存在    操作前提不成立，例如待替换文字找不到
REPAIR_CODES = {"INVALID_ARGUMENT", "UNKNOWN_TOOL", "NOT_FOUND", "PRECONDITION_FAILED"}


class ToolRecovery:
    def __init__(self, *, max_retries: int = 2, max_repairs: int = 2,
                 sleep=None, random_value=None):
        self.max_retries = max_retries  # 程序尝试的最大次数，首次执行后，最多自动重试两次
        self.max_repairs = max_repairs  # 大模型尝试的最大次数，首次调用失败后，最多有两次修正机会
        self.sleep = sleep or time.sleep
        self.random_value = random_value or random.random
        self.failures: dict[tuple, int] = {} # 某个工具，目标 的 失败次数
        self.uncertain_files: set[tuple] = set() # 状态不确定的文件
        self.events: list[dict] = []  # 每次调用的记录
    # 给工具调用确定一个计数身份
    @staticmethod
    def key(name, args, ctx) -> tuple:
        target = "<default>"
        if isinstance(args, dict):
            for field in ("path", "task_id", "id", "request_id", "service", "name", "to"):
                if field in args:
                    target = str(args[field])
                    if field == "path" and isinstance(args[field], str):
                        try:
                            target = os.path.normcase(str((Path(ctx.workdir) / args[field]).resolve()))
                        except (OSError, RuntimeError, ValueError):
                            pass
                    break
        return (ctx.owner, str(ctx.workdir), name, target)
    # 执行前判断是否应该拦住   返回None,允许继续检查和执行   返回ToolResult拦截，并说明原因
    def exhausted(self, key: tuple) -> ToolResult | None:
        # 首先检查文件状态
        if key[2] in {"write_file", "edit_file"} and (key[0], key[1], key[3]) in self.uncertain_files:
            return tool_error("STATE_CHECK_REQUIRED", "Previous file mutation has an unknown outcome. "
                              "Read the complete file before issuing another mutation to this target.",
                              action="inspect_state")
        # Initial failure plus two opportunities to repair; a successful call resets it.
        if self.failures.get(key, 0) >= 1 + self.max_repairs:
            return tool_error("RECOVERY_EXHAUSTED", "Recovery budget exhausted for this tool/target; "
                              "report the blocker or choose another appropriate approach.")
        return None
    # 根据结果更新状态 这次调用结束了，根据结果修改我的记录
    def observe(self, key: tuple, result: ToolResult) -> ToolResult:
        if key[2] in {"write_file", "edit_file"} and not result.ok and result.execution_status == "unknown":
            self.uncertain_files.add((key[0], key[1], key[3]))
        if (result.ok or result.error_code == "NOT_FOUND") and key[2] == "read_file" and result.inspected_path is not None:
            self.uncertain_files.discard((key[0], key[1], result.inspected_path))
        if result.ok:
            self.failures.pop(key, None)
        elif result.error_code in REPAIR_CODES:
            self.failures[key] = self.failures.get(key, 0) + 1
        return result
    # 当前结果能不能由程序自动重试
    def can_retry(self, tool, result: ToolResult, attempt: int) -> bool:
        #  工具存在   工具只读取信息   项目明确允许他重复执行  当前错误被标记为零时故障  还有重试机会 错误不在排除列表
        return bool(tool and tool.read_only and tool.retry_safe and result.transient
                    and attempt <= self.max_retries
                    and result.error_code not in {"PERMISSION_DENIED", "INVALID_ARGUMENT"})
    # 记录一次尝试，但不改变恢复状态
    def record(self, name, args, result, *, phase, attempt, elapsed=0.0, retry=False):
        try:
            serialized = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = repr(args)
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        self.events.append({"tool": name, "arguments_sha256": fingerprint,
                            "phase": phase, "attempt": attempt, "ok": result.ok,
                            "error_code": result.error_code,
                            "execution_status": result.execution_status,
                            "elapsed_seconds": round(elapsed, 6), "auto_retry": retry})

    def wait(self, attempt: int):
        # Full jitter, with a small bounded delay for local tools.
        self.sleep(self.random_value() * min(2.0, 0.25 * (2 ** (attempt - 1))))
