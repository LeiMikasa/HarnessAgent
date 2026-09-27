"""The tool pool: the registry plus the built-in tools.

    registry.py   Tool, ToolContext, ToolRegistry -- the dispatch machinery
    basic.py      bash, read_file, write_file, edit_file, glob, grep

Tools in other modules register into the same registry:

    agent.todo        todo_write
    agent.skills      load_skill
    agent.tasks       create_task, update_task, list_tasks, get_task, ...
    agent.subagent    task
    agent.mcp         connect_mcp, plus mcp__<server>__<tool> at runtime
    agent.team_tools  spawn_teammate, request_plan, review_plan, ...
"""

from .basic import register_basic_tools, safe_path
from .registry import Tool, ToolContext, ToolRegistry

__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "register_basic_tools",
    "safe_path",
]
