# minicode 新版工作交接召回评测

新版数据集与旧的 `recall50.json` 分开保存，不覆盖旧结果。它包含 **50 个合成工作交接场景**，并非从实际用户会话抽样的 50 个真实项目。10 种业务任务与 5 类压力条件组合，各类 10 例。

| 类别 | 检查什么 |
|---|---|
| handoff：任务交接 | 从分散的对话与工具回显中取回约定，保留路径、数字、列表、布尔值 |
| revisions：需求修订 | 识别已批准的新决定；不把后出现的未批准提案当成现行配置 |
| collisions：相似干扰 | 区分同名项目的 legacy 版本、staging 环境与目标生产配置 |
| live-files：文件状态变化 | 历史快照已过期时，读取当前工作区文件；文件未覆盖的字段仍查历史 |
| mixed：混合历史 | 同时处理修订、干扰及当前文件优先级 |

业务任务包括 Vue 音乐工作台、订单服务、文件上传、商品搜索、账单导出、通知中心、库存同步、登录网关、数据看板和后台任务队列。每例验收 **8 项有依据的信息与 2 项未知信息**。未知项应为 `null`，单独统计正确拒答，不混入事实召回率。

历史由自然语言交接、确认决定、未批准讨论、其他项目配置和无关检查记录组成。数值与噪声由固定种子生成，事实分散在不同位置。不同条件的历史长度随机落在 30～44 或 56～68 个记录片段之间；部分短历史不会触发生产压缩阈值，必须如实记录，不能把这些例子计作已验证压缩有效。

## 一个例子

`live-files-orders` 让 Agent 接手订单服务。旧交接里有端口、路由、分页、重试、幂等请求头、分支、支付方式及自动退款开关。工作区的 `config/production.json` 已由人工更新前三项，最终应读取新文件，并从历史补齐其余信息。事故负责人和签名摘要未提供，应回答 `null`。

两组任务文本、历史和初始文件完全相同。只改变 `Runtime(..., compaction_enabled=...)`，使用生产压缩阈值。公共环境说明解释历史日志已不存在、归档标记指向历史快照；它对两组相同，不提供答案、专用搜索关键词或预知的归档文件名。基础工具执行读取与检索，没有新增搜索历史工具。

## DeepSeek 真实模型接入

新版入口支持 `--provider deepseek`，通过项目已有的 Anthropic 兼容客户端直接调用 DeepSeek，使用原生工具调用。默认沿用 `.env` 中的 `MODEL_ID`、`ANTHROPIC_BASE_URL` 和密钥，也可用 `--model` 单独指定评测模型，不修改日常配置。当前项目的 `MODEL_ID=deepseek-flash`。

默认仍是一组免费的 Mock；只有显式选择 DeepSeek 才运行付费模型。可以先查看计划，不发送请求：

```powershell
# 查看五类各一个场景的计划，共 10 次 Agent 运行；不调用模型
.\.venv\Scripts\python.exe -m evals.recall_real --provider deepseek --limit 5 --plan

# 真正开始小批评测，会消耗 DeepSeek API 额度
.\.venv\Scripts\python.exe -m evals.recall_real --provider deepseek --limit 5

# 只测一个指定场景：full / compact 各一次
.\.venv\Scripts\python.exe -m evals.recall_real --provider deepseek --ids live-files-orders
```

每次 Agent 运行可能有多次模型调用。模型、服务地址、代码摘要、场景和预算保存在批次配置里，续跑时必须一致；密钥不写入批次配置。`--all` 才选择全部 50 个场景、共 100 次 Agent 运行。默认模型 ID 若是滚动别名，记录这个 ID 并不等于锁定服务端权重版本。

DeepSeek 报告记录 API 返回的实际 token 用量，包含摘要调用，不包含 Codex CLI 开销。缺失用量保持未知，不根据 token 数自动推算账单。`--max-tokens` 是传给 API 的输出上限；`--timeout` 在模型调用前检查整例剩余预算，并据此限制 SDK 等待、禁用评测请求的 SDK 自动重试，不保证强制中断已经进行的任意本地文件操作。认证、余额、配额或限流错误会停止批次；其他模型/运行时调用连续失败三次也会停止。

## Codex 真实模型接入

`evals/codex_llm.py` 只用于评测，不修改日常 DeepSeek 或 Anthropic 配置。它调用本机已安装、已通过 ChatGPT 登录的 Codex CLI。每次模型请求启动一个新的临时会话，禁用用户配置、Shell、插件、Hooks、队友、记忆和联网搜索。模型只能返回 JSON 格式的正文或工具请求，真实文件工具仍由 minicode 的 `execute_tool` 执行，权限、恢复和压缩也由 minicode 负责。

CLI 在独立空目录启动；若事件流表明它执行了自己的工具，当前调用会失败，不当作合格试验。验收答案保存于工具工作区外；输入文件与最终 `answer.json` 由独立 Python 验收。整合传输使用 JSON 形式的工具请求，不能描述成测试了原生 OpenAI API function calling。

这会消耗账号的 Codex 使用额度，不调用 DeepSeek，不保证无限免费。每轮 token 用量来自 CLI 的 `turn.completed.usage`，原值保留；包含 Codex 自身系统提示和传输格式的开销。缓存命中是输入的组成部分，不重复相加。缺失用量记为未知。`--max-tokens` 是发给模型的输出长度提示，CLI 不提供这里使用的严格同等输出上限；另有调用超时和 Agent 轮数预算。

官方能力说明：[非交互调用和结构化输出](https://learn.chatgpt.com/docs/non-interactive-mode)、[ChatGPT 登录方式](https://learn.chatgpt.com/docs/auth)。

## 运行

在项目根目录执行：

```powershell
# 仅检查计划，不调用模型
.\.venv\Scripts\python.exe -m evals.recall_real --provider codex --ids live-files-orders --plan

# 免费检查评测流程；Mock 分数不能代表模型效果
.\.venv\Scripts\python.exe -m evals.recall_real --provider mock --ids live-files-orders

# 1 组成对真实模型试跑：2 次 Agent 运行，每次可能有多次模型调用
.\.venv\Scripts\python.exe -m evals.recall_real --provider codex --model gpt-6.1-sol --ids live-files-orders --max-turns 10 --timeout 360

# 取五类各一个场景：10 次 Agent 运行
.\.venv\Scripts\python.exe -m evals.recall_real --provider codex --limit 5

# 全量需显式选择 --all：100 次 Agent 运行，可能消耗大量 Codex 额度
.\.venv\Scripts\python.exe -m evals.recall_real --provider codex --all
```

默认只运行一组 Mock，真实模式仍默认一组。续跑同一版本代码、相同参数时添加 `--resume <批次目录>`。代码或配置变动会拒绝续跑，避免混合版本。

结果位于 `.scratch/evals/realistic-<时间>-<provider>-<标识>/`：

- `report.md`、`summary.json`：事实召回、正确拒答、整例成功率、双方 token、配对比较。
- 每例 `result.json`：逐项对错、结束原因、压缩是否触发、工具调用数、输入文件是否被修改。
- 每例 `trace/timeline.md`：按调用顺序阅读模型可见回复、工具请求和结果预览，先看这个定位空转发生在哪一轮。
- 每例 `trace/model-calls/call-*/request.json`、`response.json`、`error.json`：模型输入、响应及调用异常；不包含 SDK 凭据、HTTP 请求头或验收器答案表。
- 每例 `trace/tools.jsonl`：完整工具参数和工具返回文本（工具本身的输出上限仍有效），包含未执行的参数校验和权限拒绝。
- `transport/call-*/request.json`、`events.jsonl`：真实请求与 CLI 原始事件，供本地排查，不应提交到公开仓库。

详细日志只在本地评测中记录，位于每例 `workspace` 的外面，Agent 无权通过文件工具读取它们。记录时遮蔽已配置的 API 密钥，不保存 SDK 客户端或回调。日志写入失败会记录到 `result.json` 的 `local_trace_errors`，不会将模型或工具的成功结果改成失败。日志仍包含题目与模型输出，不应提交到公开仓库。常规运行的 `RunTrace` 继续只记录指纹和统计信息。

运行时现在提供两条通用引导：先使用对话已有证据，再按真实归档标记补缺口；证据足够后提交用户要求的产物。最后三轮会提醒剩余预算，但不会自动写答案、猜未知信息或取消必要工具。这些引导对 full / compact 同时生效；题目、标准答案和评分保持不变。它们是否改善真实模型表现，需要重新跑小样本验证，离线测试不能代替这个结论。

本次代码与提示词发生变化，旧批次的代码摘要不同，不能直接 `--resume` 混跑。应新建批次，保留原失败记录，用相同场景比较。

失败或未完成的例子仍进入任务召回分母。先检查是否完成、答案是否正确、是否实际压缩，再比较双方 token 和耗时。单次试跑只能验证链路并发现问题，不能据此宣称普遍召回率或节省比例。

## 首次真实试跑记录

2026-10-21 使用 `gpt-6.1-sol`，运行 `live-files-orders` 的一组成对试验。两组均正常结束、8/8 项事实正确、2/2 项正确拒答，输入配置文件未被修改。

| 指标 | full | compact |
|---|---:|---:|
| 首次请求输入 token | 40,875 | 26,724 |
| 累计输入 token | 123,298 | 191,133 |
| 累计输出 token | 317 | 474 |
| 模型调用 | 3 | 4 |
| minicode 工具调用 | 2 | 4 |
| 耗时 | 37.845 秒 | 41.321 秒 |

压缩组第二层归档了 126 条消息，本轮未触发模型摘要，不能声称覆盖五层压缩。虽然首次模型输入减少约 34.6%，随后读取归档把大段历史带回上下文，又增加了工具校验轮次，累计输入最终增加约 55.0%。这是一次可核查的负向成本结果，不能改写成“节省 55%”。这些 token 含 CLI 自身开销，不能当成 DeepSeek 用量或直接换算 DeepSeek 费用。

原始批次：`.scratch/evals/realistic-20261021T121045Z-codex-2259b3/`。只执行了 1/50 个真实场景，其余 49 个没有真实运行。另有一次传输连通性真实调用，输入 10,970 token、输出 20 token；它不属于成对评测，未混入上表。
