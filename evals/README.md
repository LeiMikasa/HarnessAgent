# 单 Agent 与团队模式耗时评测（试运行）

## 项目自身的生命周期基线

```powershell
python -m evals.project_baseline
```

这个离线评测直接检查本项目的两条团队协作链路：一次性 CLI 在主 Agent 返回后是否等待队友，以及队友空闲等待期间收到的消息是否被处理。它使用生产 CLI 分支和生产 Teammate 工作循环，不调用付费模型，也不改生产代码。结果写入 `.scratch/evals/project-baseline-<时间>/summary.json`。`passed: false` 表示对应行为未满足预期；这是基线观察，不等于所有真实任务都会失败。CLI 检查用可观测的 Runtime 替身验证调用顺序，不声称模拟了完整团队执行或模型质量。

优先优化建议：先修复等待消息被消耗后丢弃的问题，再明确一次性 CLI 的完成语义（等待/超时/明确报告未完成），然后用同一评测复跑；最后再比较真实任务的成功率、耗时和成本。

从项目根目录运行：

```powershell
python -m evals.team_speed --provider mock
python -m evals.team_speed --provider deepseek --repeats 3 --timeout 240
```

每次试验都从相同的 `dual_modules` 初始文件复制出全新工作目录，并使用独立状态目录。两组收到完全相同的任务描述；单 Agent 组不提供团队工具，团队组要求创建任务、启动一位队友并行实现另一个文件。短生命周期 `task` subagent、shell 命令、MCP 连接、记忆和压缩在两组均关闭；计划审批和 Git worktree 也关闭，以便评测共享目录中的团队协作。工作区外的文件写入仍被拒绝。验收脚本位于工作目录之外，不会被被测 Agent 修改。

计时从 Runtime 创建前开始，到独立验收通过并关闭队友后结束；`submit()` 返回或任务标记完成都不算成功。每次运行记录总模型请求数（含队友）、工具调用数、任务状态、队友状态、验收结果及耗时。结果保存在 `.scratch/evals/<batch>/summary.json`，试验工作目录保留供排查。模型 token/费用暂不可从现有 `LLMResponse` 获取，不应把请求数当作费用。

`mock` 只验证评测器流程，耗时毫无模型性能意义。真实模型至少应重复 3 次、交错两组执行，且仅在两组都验收通过、团队组确实使用了队友时比较成对耗时。本例仅含一个小任务，不足以代表团队协作的一般收益；后续应增加不同大小、不同可并行度的任务，并统计成功率和失败类型。运行真实模型会消耗 API 额度。

## 验证反馈对照

```powershell
python -m evals.verification_feedback --provider mock
python -m evals.verification_feedback --provider deepseek --repeats 3 --max-repairs 2
```

该评测使用独立的 `csv_line.py` 任务和工作区外的验收脚本。两组在第一次失败后都有最多两次修复机会：`generic` 只收到相同的“验收失败，请修复”消息；`diagnostic` 额外收到具体失败用例。除此之外，模型、初始文件、任务描述、工具和预算相同。记录第一次通过率、最终通过率、失败后停止次数、修复轮数、耗时与模型调用量。`mock` 故意写出一个错误实现，用于证明反馈确实能进入下一轮；它不能证明真实模型会受益。如果真实模型两组都首次通过，这个任务没有触发反馈机制，结论应是“不确定”，而不是“反馈无效”。

为了保证真实模型也会遇到失败路径，可以改跑已知缺陷修复场景：

```powershell
python -m evals.verification_feedback --provider mock --scenario seeded-repair --bug-case all
python -m evals.verification_feedback --provider deepseek --scenario seeded-repair --bug-case all --repeats 3
```

三个用例从同一份正确解析器分别注入“遗漏末尾空字段”“转义引号处理错误”“接受非法引号”之一。每个用例的两组从**完全相同的有缺陷文件**开始，外部验收事先确认失败；首次 Agent 回合收到相同任务与失败通知，唯一区别仍是是否附具体失败用例。这测量的是“面对已知失败时，诊断信息能否帮助修复”，不能与从零实现的首次通过率混为一谈。`--bug-case` 可指定其中一个用例；默认 `all`。
