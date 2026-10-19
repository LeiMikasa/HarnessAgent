from agent.runtime import Runtime


runtime = Runtime.create(
    provider="mock",
    workdir=r"D:\HarnessAgent\.scratch\docker-demo",
    sandbox_enabled=True,
    teams_enabled=False,
    memory_enabled=False,
)

try:
    output = runtime.execute_tool({
        "type": "tool_use",
        "id": "sandbox-test",
        "name": "bash",
        "input": {
            "command": "pwd && python3 --version && node --version",
        },
    })

    print("工具是否成功：", output.ok)
    print(output)

finally:
    runtime.close()