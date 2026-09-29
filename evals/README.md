# Agent 任务评测（试运行）

## 上下文压缩对照评测

已完成的 15 组真实模型对照试验见 [评测报告](../docs/上下文压缩评测报告.md) 和 [逐次运行数据](results/context-compaction-2026-09-29.json)。报告注明了验收规则修正及结论边界。

```powershell
# 免费离线烟测：检查两组任务、压缩触发、归档检索和结果记录
python -m evals.context_compaction --provider mock

# 真实模型试验：会消耗 API 额度，先配置 .env 并通过 preflight.py --tools
python -m evals.context_compaction --provider deepseek --repeats 3 --timeout 240
```

还可运行 `python -m evals.context_compaction --provider mock --case summarization`：这组把准确值放在超长普通历史消息中，使前几级工具结果处理无法解决容量问题，从而触发模型摘要。比较时需把额外的摘要模型调用计入总请求量；若摘要丢失准确值，独立验收会失败。真实模型可分别用 `--case coding`、`--case retention`、`--case summarization` 分批运行，避免一次启动全部场景；`--case all` 则运行三种场景。

脚本包含三类用例：`coding` 要求在长历史之后完成 CSV 解析器或双文件工具函数，由工作区外的验收脚本检查；`retention` 把按固定种子生成的准确值放在历史工具输出的不同位置，最终请求不重复该值；`summarization` 把准确值放在超长普通消息的开头、中间或末尾，要求模型摘要后仍能找回。后两类都由工作区外的检查要求输出完全一致的内容。每个用例都有 `full`（不压缩）与 `compact`（自动压缩）两组。成对试验使用相同的模型、初始文件、历史记录、工具集合、轮数预算和验收标准，每次复制新工作区；两组执行顺序交替。历史默认包含 28 组工具调用及结果，每个结果约 5000 字符。可用 `--pairs`、`--result-chars` 调整压力，用 `--case` 单独运行。正式样本可用 `--case all --repeats 5` 运行 3 类 × 5 次 = 15 组成对试验（30 次 Agent 运行）；其中编码任务交替使用两道题，不能将重复试验描述为 15 道不同任务。

每次试验写入 `.scratch/evals/context-<时间>/repeat-*/result.json`，整批汇总在 `summary.json`。优先看独立验收成功率、上下文超限次数，再比较双方都成功的成对耗时。`input_chars_sum` 和 `input_chars_max` 是发送给模型的系统提示、消息及工具定义的**字符量**，可用于比较上下文负载；它们不是 token 数或费用。`summary_calls`、`archive_files`、`tool_result_files` 和 `compaction_triggered` 可确认是否真的触发压缩。若压缩没有触发，脚本会提示增加历史规模。

`mock` 使用固定行为的模拟模型，只证明评测流程可运行，不能据此推断压缩提高完成率或节省真实模型成本。真实模型至少重复多次，并扩充不同任务、历史长度与有效信息位置；当前三类任务仍是合成历史，不能代表所有长任务。每组成功率按独立验收通过且 Agent 正常结束的次数除以总运行次数计算；超时、报错和验收失败都计为失败。对于压缩组，如果召回准确值失败，应检查中段归档是否把关键信息移出了模型视野、归档路径是否可读取，以及模型是否实际执行了检索。摘要模型调用也是成本的一部分，不能只看主循环轮数。

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
