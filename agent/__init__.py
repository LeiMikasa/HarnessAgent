"""HarnessAgent -- a modular coding-agent harness.

Agency comes from the model; this package is the vehicle.  Every module here
gives the model one more capability inside a single, unchanged agent loop:

    loop.py          the loop itself (the only thing that never changes)
    llm.py           provider abstraction (+ an offline mock model)
    events.py        hooks around the loop
    permissions.py   three-gate permission pipeline
    tools/           the tool pool, assembled from independent modules
    context.py       context compaction
    memory.py        cross-session memory
    tasks.py         file-backed task graph
    teams.py         persistent teammates, mailbox, task-bound worktrees
    mcp.py           external capability routing
    skills.py        on-demand knowledge loading
    prompt.py        system prompt assembly
    cli.py           terminal entry point
"""

__version__ = "0.1.0"
