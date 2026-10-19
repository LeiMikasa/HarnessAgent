from agent.runtime import Runtime


runtime = Runtime.create(
    provider="mock",
    workdir=r"D:\HarnessAgent\.scratch\docker-demo",
    sandbox_enabled=True,
    teams_enabled=False,
    memory_enabled=False,
)

try:
    result = runtime.execute_tool({
        "type": "tool_use",
        "id": "read-check",
        "name": "read_file",
        "input": {
            "path": "read-demo.txt",
            "offset": 2,
            "limit": 1,
        },
    })

    print("读取成功：", result.ok)
    print("读取内容：", result)
    print("完整读取标记：", result.inspected_path)

finally:
    runtime.close()