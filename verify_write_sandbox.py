from agent.runtime import Runtime


runtime = Runtime.create(
    provider="mock",
    workdir=r"D:\HarnessAgent\.scratch\docker-demo",
    sandbox_enabled=True,
    teams_enabled=False,
    memory_enabled=False,
)

content = "第一行：你好。\n第二行：sandbox works"
path = "write-demo/nested/hello.txt"

try:
    written = runtime.execute_tool({
        "type": "tool_use",
        "id": "write-check",
        "name": "write_file",
        "input": {
            "path": path,
            "content": content,
        },
    })

    print("写入成功：", written.ok)
    print(written)
    assert written.ok

    read = runtime.execute_tool({
        "type": "tool_use",
        "id": "read-check",
        "name": "read_file",
        "input": {"path": path},
    })

    print("读取成功：", read.ok)
    print(read)
    assert read.ok
    assert str(read) == content

    print("验证通过：写入和读回的内容一致")

finally:
    runtime.close()