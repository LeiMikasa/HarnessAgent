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
- `transport/call-*/request.json`、`events.jsonl`：真实请求与 CLI 原始事件，供本地排查，不应提交到公开仓库。

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
