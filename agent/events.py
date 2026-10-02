"""Hooks -- extension points around the loop, never rewrites of it.

Four points matter in an agent loop, and every lesson in the course attaches
itself to one of them rather than editing the loop:

    UserPromptSubmit   the user's text arrives, before it reaches the model
    PreToolUse         a tool call is proposed; return non-None to block it
    PostToolUse        a tool call finished; observe only
    Stop               the model produced no tool call and wants to stop

Hooks are plain callables.  `trigger` returns the FIRST non-None result, which
is what makes a PreToolUse hook able to veto a call by returning a reason
string.  `collect` runs every hook and gathers all results, for observers.

    registry.register(PRE_TOOL_USE, permissions.check_hook)
    registry.register(STOP, memory.remember_after_turn)

The registry is an instance, not a module global, so every teammate and every
subagent can carry its own hook set without cross-talk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

# Event names.
USER_PROMPT_SUBMIT = "UserPromptSubmit" # 用户文本到达，还没送给模型
PRE_TOOL_USE = "PreToolUse" # 模型提出工具调用，尚未执行
POST_TOOL_USE = "PostToolUse" # 工具调用完成
STOP = "Stop"  # 模型不再调用工具，想结束
SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"

DEFAULT_EVENTS: tuple[str, ...] = (
    USER_PROMPT_SUBMIT,
    PRE_TOOL_USE,
    POST_TOOL_USE,
    STOP,
    SESSION_START,
    SESSION_END,
)

Hook = Callable[..., Any] # 类似：(*args: Any, **kwargs: Any) -> Any，定义了一个类型别名是Hook


@dataclass(frozen=True)
class StopDirective:
    """A Stop hook may continue the loop or return a non-final status."""

    action: str  # continue | return
    message: str = ""
    stop_reason: str = "final"


class Hooks:
    """An ordered registry of hook callbacks, keyed by event name."""

    def __init__(self, events: Iterable[str] = DEFAULT_EVENTS):
        self._events: dict[str, list[Hook]] = {name: [] for name in events}

    # -- registration -------------------------------------------------------
    # 把回调加到某个事件下，并返回该回调。
    def register(self, event: str, callback: Hook) -> Hook:
        """Add `callback` to `event`.  Returns the callback for decorator use."""
        self._events.setdefault(event, []).append(callback)
        return callback
    # 装饰器,注册相关方法
    def on(self, event: str) -> Callable[[Hook], Hook]:
        """Decorator form: ``@hooks.on(PRE_TOOL_USE)``."""

        def decorator(callback: Hook) -> Hook:
            self.register(event, callback)
            return callback

        return decorator
    # 事件中移除
    def unregister(self, event: str, callback: Hook) -> None:
        handlers = self._events.get(event)
        if handlers and callback in handlers:
            handlers.remove(callback)
    # 清空某个事件
    def clear(self, event: str | None = None) -> None:
        if event is None:
            for name in self._events:
                self._events[name].clear()
        else:
            self._events.setdefault(event, []).clear()
    # 返回该事件的回调列表副本
    def callbacks(self, event: str) -> list[Hook]:
        return list(self._events.get(event, ()))

    # -- firing -------------------------------------------------------------
    # 按顺序运行hook,遇到第一个非None结果就停止并返回它
    def trigger(self, event: str, *args: Any) -> Any | None:
        """Run hooks in order; stop and return the first non-None result.

        A non-None result means "this hook has an opinion" -- for PreToolUse
        that vetoes the call, for Stop it forces another turn.
        """
        for callback in self._events.get(event, ()):
            result = callback(*args)
            if result is not None:
                return result
        return None
    # 运行所有Hook收集所有非None结果
    def collect(self, event: str, *args: Any) -> list[Any]:
        """Run every hook and gather all non-None results."""
        results = []
        for callback in self._events.get(event, ()):
            result = callback(*args)
            if result is not None:
                results.append(result)
        return results


# A process-wide default registry, used when a component is not handed one.
DEFAULT_HOOKS = Hooks()
