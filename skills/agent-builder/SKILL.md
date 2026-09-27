---
name: agent-builder
description: Scaffold or extend an agent harness — a new tool, a new subagent, a new hook, or a whole new loop — following the design used by this project.
---

# Agent Builder

Use this skill when the task is "add a capability to the agent" rather than
"do a task with the agent".

## The one rule

**Agency comes from the model. The harness gives it a place to land.**

The loop never changes. Everything you add is either a tool the model can call,
a hook around the loop, or context the model can read. If you find yourself
adding a branch to `agent/loop.py`, stop and look for the hook you are missing.

```
             +---------------------------+
             |        the loop           |   <- do not edit
             +---------------------------+
                |    |    |    |    |
             tools hooks memory tasks teams   <- add here instead
```

## Adding a tool

1. Pick the module. One mechanism per module: `todo.py`, `tasks.py`, `mcp.py`.
2. Write the handler with the standard signature:

   ```python
   def run_my_tool(args: dict, ctx: ToolContext) -> str:
       ...            # return a string; never raise — errors are data
   ```

   `ctx` carries `settings`, `workdir`, `owner`, `interactive`, and `runtime`.
   Resolve every path through `safe_path(ctx, path)` so the workspace boundary
   is enforced in one place.

3. Write the JSON schema. Describe *when* to call the tool, not just what it
   does — the description is the model's only documentation.
4. Write `register_my_tools(registry)` and call it from `Runtime.__init__`.
5. Add tests to `tests/`. Drive the tool through the loop with a `MockLLM`
   script, not by calling the handler directly.

## Adding a hook

- `UserPromptSubmit` — rewrite or log the user's request before the model sees it.
- `PreToolUse` — inspect the proposed call. Return a string to **veto** it; the
  string becomes the tool result. Return `None` to allow.
- `PostToolUse` — observe the output. Cannot change it.
- `Stop` — the model produced no tool call. Return a string to force another
  turn; return `None` to let the loop end.

Hooks run in registration order and the first non-`None` result wins. Never do
slow work on `PreToolUse`; the whole loop is waiting.

## Adding a subagent

Prefer a tool that calls `Runtime.spawn_subagent`, which runs the shared
`run_loop` with a fresh `messages[]` and a restricted tool pool. Exclude anything
recursive (`task`, `spawn_teammate`) or the cost becomes unbounded.

## Adding context

If the model needs information, prefer **on-demand** loading over injecting it
up front. A skill catalog costs one line per skill; the body costs thousands of
tokens. Load bodies only when they apply.

## Before you call it done

- [ ] The loop in `agent/loop.py` is unchanged.
- [ ] Every new tool returns a string and never raises.
- [ ] Paths go through `safe_path`.
- [ ] A test drives it through the loop with a mock model.
- [ ] The full suite still passes: `python -m unittest discover -s tests`.
