# Real RSI 进度总结（截至 2026-09-22）

> 本文件是 Claude 文档《Real RSI 进度总结（截至 2026-09-22）》的 markdown 副本。文档为准；如两者不一致，说明文档之后又被编辑过。
> 它衔接并大幅扩展了旧文档《Real RSI 进度总结（截至 2026-09-20）》（`real_rsi_progress_20260920.md`）——那份文档到 pilot/v2 阶段为止，本文档从 improver 重设计开始往下讲。

## 方法与实现

### 方法总览

Real RSI 是包在现有 AIDE（rule/agent 两种模式）外面的一个最小自我改进循环，不重写 AIDE 本身：

```
任务包 → 现有 AIDE runner（rule|agent）+ 可变 harness H_t
     → 轨迹 + 决策事件 → verifier bank（reward vector，从不合并成单一分数）
     → 记忆更新 → improver 在独立 workspace 里直接编辑 harness 文件
     → 范围/可运行性检查 → H_{t+1}（diff 只是事后记录）
```

**四条贯穿全局的设计原则**：
1. **AIDE 本体、任务数据、evaluator 从不被改动**。harness 只能通过 AIDE 已经暴露的通道（环境变量 + notes 文件）送达，从不直接改研究 repo 代码。
2. **reward 从不被折叠成单一分数**。verifier bank 返回原始向量，每个 verifier 独立发声，不互相抵消。
3. **训练/测试纪律是硬约束，不是约定**（exposure ledger + 代码层硬检查），防止 held-out 的官方成绩泄露给 meta-improver。
4. **每一步都可重放/可审计**：round 目录、harness 版本、memory jsonl 都是磁盘上的纯文件，任何步骤都能单独重跑。

实现在独立小 repo `real-rsi-mvp/`（嵌在大 repo 里），不改大 repo 一行被跟踪的代码；研究仓库自己的 issue scanner 被未修改地向量化拷贝进来（`vendor/issue_scanner/audit_run.py`，测试守着 sha256）。

### Harness：可变对象与边界

**harness 是一个文件目录**（`harness.ALLOWED_FILES`，按职责划分不按后缀）：

| 文件 | 职责 |
|---|---|
| `prompt_notes.md` | 追加到任务描述里的文本，代码生成（及 agent 模式决策）prompt 都会读 |
| `decision_policy.md` | 仅 agent 模式：选什么/提交什么的指导文本 |
| `rule_config.json` | 仅 rule 模式：白名单、有界的数值旋钮（max_stagnation 等） |
| `memory_policy.json` | 记忆选取数量/长度上限，以及是否把 lessons 直接注入 prompt |
| `observation_policy.json` | 能不能打开 AIDE 已有的观测开关（见下文） |
| `CHANGES.md` | improver 自己的说明文本，不影响行为 |
| `hooks/select_memory.py`、`hooks/render_notes.py` | 可选脚本，接管"怎么选记忆"、"怎么组织最终 notes"这两个职责，见下一节 |

AIDE 本体、任务数据、evaluator 不在边界内，任何想指向它们的改动都会被拒。

**版本存储**（`HarnessStore`，`harness_versions/<task>/<mode>/H<n>/`）：每个版本一个目录，含 `harness/`（文件本身）、`version.json`（元数据+sha256）、`diff.patch`（对父版本的文件级 unified diff，**只是事后记录**，从不是输入）、`rendered_notes.txt`（commit 时渲染一次并固化，之后永远不重新渲染）。每个版本都提交到 git 并打标签 `harness/<task>/<mode>/<version>`。

**如何送达 AIDE（不改研究 repo）**：`runner.py` 只用研究 repo 自己暴露的环境变量通道（均在 `scripts/_common.sh` 的 `_CALLER_OVERRIDE_VARS` 白名单里，测试守着）：notes/decision policy/记忆 lessons → 内容寻址、不可变的 `config/tasks/<comp>.notes.rsi_<hash>.txt` + `PROMPT_VARIANT`（H0 渲染为空，是未被碰过的 stock 对照）；rule_config → `AIDE_MAX_STAGNATION`/`AIDE_DEBUG_PROB`/`AIDE_MAX_DEBUG_DEPTH`/`AIDE_EXTRA_KWARGS`；模式 → `AIDE_SELECTION_MODE=rule|agent`；观测策略 → `AIDE_SUB_STATS`（这个不在白名单里，改成 collect 时实时验证，见下文）。

### Improve 流程：agent 直接编辑 harness

（2026-09-20 重设计：从"improver 输出一个有边界的 JSON patch，程序应用"改成"improver 在独立副本里直接编辑文件，框架事后检查"。）

1. `loop.improve` 为这一轮建一个独立 workspace：`harness/`（当前版本的可写副本）+ `context/`（只读证据：`SUMMARY.md` 人读概览、`runs.json` 完整 verifier 详情、`memory.json`、`history.json`（之前每次 improve 会话改了什么、对应版本得分如何）、`INSTRUCTIONS.md`（契约文本）。
2. `AgentImprover`（任何具备 `chat_tools(messages, tools)` 的 LLM）拿到 `list_files`/`read_file`（分页）/`write_file`/`delete_file`/`check`/`finish` 六个工具，自由边寻边改。没有"一次一个组件"、"最大追加 N 字"的限制，可设 `max_steps`、`max_cost_usd`。
3. 会话结束后，框架检查**范围**（只能改 `ALLOWED_FILES`、`context/` 未被碰、尺寸/数值边界/禁用文本）和**可运行性**（JSON 能解析、hook 过静态检查、在候选版本自己的 round 和它将看到的 memory 上真能渲染且渲染两次结果一致）。通过才成为 H_{t+1}，状态四选一：`proposed`/`no_change`/`rejected`/`error`。
4. `diff.patch`、`improver_record.json`（摘要/对话记录/scope 报告/token 用量）、`rendered_notes.txt` 随新版本写入，都只是事后记录。

旧的单个 JSON patch 路径保留为 `--improver patch`（`PatchImprover`，包装 `meta_improver.propose` + `apply_patch`，仍走同一套 workspace 检查）。`MockImprover` 做确定性直接编辑供离线测试。

**hook 脚本沙箱**（`hooks/select_memory.py` 决定选哪些记忆，`hooks/render_notes.py` 决定最终 notes 怎么组织，两者都是可选的，不存在就用默认规则）：AST 静态策略只允许纯文本/数据处理库（json/re/math/textwrap/collections/itertools/statistics/string），禁模块内省/frame/`_` 开头属性/`hash`/`id`/类定义；子进程隔离（清空环境、空目录、超时、CPU/内存/文件描述符 rlimit）。**这是纵深防御，不是对抗恶意行为的安全边界**。

### 证据管线：verifier、阶段化证据、观测策略

**Verifier bank**（`rsi_mvp/verifiers/`）：包装研究 repo 现有的 issue scanner（未改动），按检测器 id/层级把每个 finding 路由到 6 个已实现的 verifier：`validation_argmax_anchoring`（提交了一个 phantom 节点）、`submission_sanity`（提交异常，如常数预测）、`validation_mirage`（预处理泄漏等——节点级区间奖励，报 [下界,上界]，因模式可能重叠无法求并集，**从不取中点**）、`extraction_distortion`、`simplified_error_attribution`、`fallback_to_runnable`。第 7 个 spec verifier `decision_insensitive_prompting` 需要成对 A/B prompt run，未实现，报 `not_applicable` 而不是静默丢弃。扫描器报了但没 verifier 认领的 finding 进 `unmapped_detectors`，让扫描器升级时新增的检测器可见。**reward vector 从不被折叠成单一分数**。

**阶段化证据**（`rsi_mvp/stage_trace.py`，2026-09-21 新增）：只要泄漏字段检测器定位过某个字段，collect 时（仅 train 任务）就重建链条——字段首次使用 → 测试端报错 → 修复方式 → 验证结果 → 测试预测质量 → 最终选择——并在每一步在决策 LLM 的 prompt 日志上实测（不是假设）它实际能看到什么。进入 improver 的 `SUMMARY.md`/`runs.json`，让它能区分"指令不够具体"与"信息从未展示"。

**观测策略**（`observation_policy.json`，2026-09-21 新增）：harness 可选打开 AIDE 已有的 `AIDE_SUB_STATS`，让候选自己提交文件的数值画像（n_unique/std/...）追加到该节点输出里，只读候选自己的预测，从不读标签/分数/grader。该开关不在研究 repo 的白名单里，所以 collect 时双向实时验证（开了必须真见到，没开不得出现）。实测发现：该画像只进反馈 reviewer 的 prompt，进不了 submit 决策 prompt，要让决策方真正看到得靠 reviewer 在 findings 里复述。

### 记忆、训练/测试纪律、成本与安全

**记忆**（`rsi_mvp/memory.py`）：每轮每个触发的 verifier 写一条 `{situation, decision_outcome, verifier, reward, evidence, lesson}` 记录，`lesson` 来自一个固定表（确定性的，可复现，不依赖 LLM 心情）；没有 verifier 触发也写一条 clean 记录。`memory_policy.render`（none|lessons）控制 lessons 是否直接注入 agent 的 prompt，默认 none——为了不让"记忆自动注入"和"补丁本身的效果"混在一起无法归因。improver 可以自己把 `render` 打开（2026-09-21 用户决定允许），代价是同一条建议会在 notes 里出现两遍。

**训练/测试纪律**（`rsi_mvp/guards.py`）：`assert_train` 在任何"官方分数要进 improver 输入"的地方硬检查 `role=='train'`；每次暴露写入 append-only 的 exposure ledger；held-out 报告前重新检查 ledger，任何 test 任务/赛题出现过就拒绝。**Freeze**：固定某个 harness 版本+它当时会用到的 memory 快照（渲染结果也一并固化），之后训练不可再改。

**成本与安全控制**：`tools/cost_watchdog.py` 按 token 日志实时统计 list-price 花费，达上限就 `qdel` job；`OpenAIChat`/`AgentImprover` 都能设 `max_cost_usd`，到线拒绝再发调用；每个 improve 会话都是独立 workspace，改坏了不会影响 H_t 本身（`rejected` 状态，不产生新版本）。完整 spec→代码对应表、CLI 用法、已知限制见 `README.md`；原始设计规格见 `docs/real_rsi_engineering_spec_v0.3.md`（**已部分过时**——写于此次重设计之前，还是 JSON patch 方案，没有 hook/观测策略/阶段化证据）。

## 一页结论

工程部分已经做完并在 CRC 上真实跑通；截至 2026-09-22，**仍然没有一个能证明 RSI 方法本身有效的实验结论**，但比 09-20 那份总结进了一大步：agent 模式的决策 bug 已经修复并验证生效，improver 已经从「输出被程序应用的 JSON patch」重构成「在独立 workspace 里直接编辑 harness 文件」，并且第一次真实用这套新流程跑出了一个 H1、做了一次 H0 对 H1 的配对实验、对一次真实失败做了完整的机制分析。

- **能站得住的**：管线端到端可用（agent 模式决策 LLM 真正在决策，不再是悄悄回退成 rule）；直接编辑 harness 的 improver 对真实模型验证通过；harness 边界、观测策略、成本上限、防污染守卫都在真实 run 上验证过，不是纸面设计。
- **还不能说的**：这个固定的 H1 是否真的提升 AUC 或降低失败率——两对配对实验的数据在统计上几乎没有约束力（差值符号相反），样本量远不足以回答这个问题。任何「自动改进方法本身有效」的结论都不成立。
- **总花费**：项目累计约 **$409**（按标价、无缓存折扣）。其中 pilot+v2 阶段 $401.51（09-20 已叫停）；本文档新增的 v3 系列（决策 bug 修复后重启的实验，用户设定 $10 上限）花了约 **$7.45**，在预算内，CRC 上当前没有任何 job 在跑。
- **唯一悬空的动作项**：一个「reviewer 是否会提示退化预测画像」的重放测试脚本已写好并预注册，但因本机 VPN/DNS 故障没能提交，尚未产生任何真实调用。

## 时间线与花费总览

| 日期 | 阶段 | 事件 | 花费 |
|---|---|---|---|
| 09-19/20 | Pilot | ROAP，rule+agent 各 3 run，H0→H1→H2 | $252.63 |
| 09-20 | v2 | 18-run 计划只跑成 6 个，发现 agent 决策 LLM 从未成功过，用户叫停 | $148.88 |
| 09-20 | 报告 | 写完《截至 09-20 进度总结》（rev 14），CRC 无 job | $0 |
| 09-20 | 重设计 | improver 从 JSON patch 改成直接编辑 harness 文件（workspace/沙箱/scope检查） | $0（离线） |
| 09-20 | 外部 review | 修 hook 沙箱漏洞、渲染不可复现、检查用错轮次、长度上限 4 个发现 | $0（离线） |
| 09-20 | v3 | 用户要求只跑一次 agent，上限 $10；修好决策 bug，专用 overlay，真实 run + improver 会话 | $1.30 |
| 09-20 | 机制验证 | 用旧失败证据跑一次真 AgentImprover，产出 H1 | $0.05 |
| 09-21 | v3_pairs | 预注册后跑 H0 vs 固定 H1 两对，发现一次泄漏导致的完全失败 | $5.99 |
| 09-21 | 免费分析 | 用户纠正两个统计声明，免费核对+修 replicate 元数据 bug+失败机制追迹 | $0 |
| 09-21 | 观测策略 | AIDE_SUB_STATS 纳入 harness，阶段化证据，实测验证 | $0.11 |
| 09-21→22 | 悬空 | reviewer 重放脚本已写完，因本机 VPN/DNS 断开未能提交 | $0 |

**v3 系列小计（用户 09-20 设定的 $10 上限内）：$7.45**。项目总计：**$408.96**。

## Pilot 与 v2 阶段回顾（旧发现，未变）

实现了一个最小 RSI 循环：任务包 → 现有 AIDE runner（rule|agent）+可变 harness → 计划、记忆、元改进器 → 新版 harness。独立小 repo `real-rsi-mvp/`，不改大 repo 一行代码。

**三个关键发现（均在研究仓库，未在这之前修复）：**
1. **agent 模式决策 LLM 从未真正跑过**：`_inject_agent_decision.py::_ordered_selection_prompt` 在无 harness bundle 的默认分支把 `[(标题,内容)]` 元组列表直接塞进 prompt，后端 `.strip()` 报错，决策在发出前就失败并回退成 rule 策略。pilot 三个 agent run 决策成功 0 次。
2. **metric-guard 把 `5-fold` 里的 5 当成指标值**：`_number_after_metric` 取最后一个指标名之后的第一个数字，把 `Mean AUC score (5-fold CV): 0.62` 读成 5.0，干扰了 agent H0/H2 共 27 个节点的反馈。已在专用 overlay 上修复并真实 job 验证，**没有**应用到共享 overlay。
3. **扫描器每种（检测器，模式）每轮只保留 1 个示例 finding**，真实命中数在 `[共 N 个节点命中此模式]` 里。初次把示例数当成命中数，造成 mirage 占比计错，后已订正。

**v2** 换了专用 overlay（含修复后的 metric-guard）、节点级区间奖励、固定中性 first draft，但 agent 臂的 9 个 run 同样因发现 2 同样不有效。用户叫停时 CRC 上只有 2 个 job 跑完，其余 qdel，未 freeze、未跑 held-out。

详情：仓库内 `docs/results_roap_h0_h1_h2.md` 与 `docs/real_rsi_progress_20260920.md`（旧 Claude 文档 rev 14 的 markdown 副本）。

## Improver 重设计：直接编辑 harness 文件

用户要求改掉原来「improver 输出一个有边界的 JSON patch，程序应用」的设计，改成「improver 在一个独立副本里直接编辑 harness 文件，框架事后检查范围和可运行性」。

**harness 现在是一个文件目录**（`ALLOWED_FILES`，按职责划分不按后缀）：`prompt_notes.md`、`decision_policy.md`、`rule_config.json`、`memory_policy.json`、`observation_policy.json`（后增，见下文）、`CHANGES.md`，加两个可选脚本 `hooks/select_memory.py`、`hooks/render_notes.py`。

**流程**：`loop.improve` 建一个独立 workspace（`harness/` 可写副本 + `context/` 只读证据：`SUMMARY.md`、`runs.json`、`memory.json`、`history.json`）→ `AgentImprover` 用 list/read/write/delete/check/finish 工具直接改文件，没有「一次一个组件」、「最大追加 N 字」的限制 → 框架检查 scope（只能改允许文件、context 未被改、尺寸/边界/禁用文本）和 runnability（JSON 能解析、hook 过静态检查、的确能渲染出 notes）→ 通过才成为 H_{t+1}，状态 `proposed`/`no_change`/`rejected`/`error` 之一。`diff.patch`（文件级）和 `improver_record.json`（摘要/对话记录/scope 报告）只是事后记录，不是输入。

**hook 脚本沙箱**（`rsi_mvp/hooks.py`/`hook_runner.py`）：AST 静态检查（只允许纯文本/数据处理库，禁 `_` 开头属性、frame/类层级内省、`hash`/`id`）+ 子进程隔离（清空环境、空目录、`RLIMIT_NOFILE=3` 让即使有 Python 层漏洞也开不了文件/socket）。**这是纵深防御，不是安全边界**。

**外部 review（09-20）发现 4 个问题，均已修复并加回归测试**：
1. hook 能通过 `collections._sys.modules["os"]`、frame 链、`string.Formatter.get_field` 漏洞到宏模块 → 改成只看到模块 facade（只暴露公开非模块属性）。
2. `python -I` 会忽略 `PYTHONHASHSEED`，同一 hook 每次渲染不同 → 去掉 `-I`、禁用 hash/id，**渲染结果现在随版本固化存为 `rendered_notes.txt`**，以后永不重跑 hook。
3. 可运行性检查固定用 round_=0 验证 → 改成用候选版本自己的真实 round 和它将看到的 memory。
4. 默认渲染路径没检查总长度上限 → 两条渲染路径改成共一道最终关卡。

旧的单个 JSON patch 路径保留为 `--improver patch`（兼容），旧 137 测试全数保留。截至本文档，统一套接口下新旧改进器共计 **193 个离线测试均通过**。

## v3：修好决策 bug 后的第一次真实 agent run

用户要求：重启实验，只跑一次 agent，成本不能超过 $10。

**修复只改专用 overlay，不改共享环境**：`overlays/v3` = v2 overlay + `patches/apply_agent_decision_fix.py`（将有 bug 的 `out.update(channel_sections)` 改成 `for entries in channel_sections.values(): for title, value in entries: out.setdefault(title, value)`）。共享 overlay 仍带着这个 bug。

**预算控制**：预算从 500 节点/4小时收紧到 50 节点/1小时（`experiments/v3/tasks`）；写了 `tools/cost_watchdog.py`，按 token 日志实时统计花费，达到上限就 `qdel`；`OpenAIChat` 加了 `max_cost_usd`，improver 端也能设上限。

**结果**：烟测（3 步，$0.096）确认决策 LLM 100% 成功，0 次回退；真实 run（job 1460025，$1.185）1 小时里 22 个节点（10 有效/12 buggy），31 次决策全部由 LLM 做出，0 次回退，官方 AUC **0.65121**。看门狗上限 $7.5，从未触发。限制这个 run 的是墙钟时间而不是成本：57 分钟里 52 分钟在执行候选代码（平均 143 秒，2 次撞上 600 秒超时）。

然后在同一 H0 上跑了一次真实 `AgentImprover`（$0.014）：6 个 verifier reward 全为 0（没检测到失败模式），它诚实地返回 `no_change`，没有产出 H1。

为了验证「写文件→check→提交」这条路径对真实模型能跑通，又在一个 repo 外的 scratch state root 上，拿 v2 agent 第 0 轮的旧失败证据（mirage 命中区间 [0.108, 0.174]，`fallback_to_runnable` -0.25）重跑一次（$0.054）：gpt-4o 6 次调用后实际写了 3 个文件（`prompt_notes.md`、`decision_policy.md`、`memory_policy.json`），`check()` 通过，框架提交为 **H1**。这个 H1 就是下一节 v3_pairs 的实验对象。顺带发现：它把 `memory_policy.render` 从 none 改成了 lessons（框架允许但报 confound 警告），导致同一条建议在 notes 里出现两遍；另外修了一个 bug：文件无结尾换行符时 `diff.patch` 会粘连成错误格式。

详情：`docs/v3_single_agent_run_20260920.md`。

## v3_pairs：H0 对固定 H1 两对配对实验

用户建议的预注册设计：固定同一 H1（上一节那个）对固定 H0，目标只验证「整个 H1 值不值得继续研究」，不拆解 notes/policy/memory 各自贡献。预注册写入 `experiments/v3_pairs/PREREG.md`（commit 8946564，先于任何 run）：25 节点、同 seed、两臂只差 `PROMPT_VARIANT`（提交前用 dry-run 差环境确认过），每 run 上限 $1.75，有停止规则（第一对失效就停；H1 明显变差就停）。

**结果（无一个被剔除）**

| 对 | 臂 | 官方 AUC | mirage 命中节点 | 成本 |
|---|---|---|---|---|
| 1 | H0 | **0.5000** | **16/25**（64%） | $1.43 |
| 1 | H1 | 0.6030 | 0/25 | $1.60 |
| 2 | H0 | 0.6679 | 0/25 | $1.53 |
| 2 | H1 | 0.6439 | 0/25 | $1.44 |

AUC 差值（H1−H0）：第一对 +0.103，第二对 -0.024。四个 run 均有效（0 回退、0 被看门狗杀）。花费共 $5.99。

**第一对 H0 的完全失败**：决策 LLM 选中一个 CV AUC=1.0 的节点（实际上是泄漏），提交变成 1162 行全同一常数，官方 AUC 掉到 0.5。详见下一节机制分析。

**免费核对（用户要求，09-21，未花任何钱）**：从原始 run 目录重算分数/成本/决策次数/配置/notes 送达，与表格全部对上。发现一个元数据 bug：`rsi collect` 没有 `--replicate` 参数默认写 1，导致第二对的两条 `run_record.json` 都标成 replicate 1。根因已修（`collect_round` 现从提交记录查真实 replicate），记录已订正为 2 并保留原值（`experiments/v3_pairs/CORRECTIONS.md`）。

**用户指出并让我撤回的两个误判断**：
1. 原报告说「可分辨差值约 0.055」，那个 sd=0.028 来自另外 7 个不同配置的 run，它自己的 95% 区间是 [0.018, 0.062]，与这四个 run（sd=0.074）不像。两对差值均值的 95% t 区间（自由度=1，只两个样本，分布假设无法检验）约 ±0.8，几乎没有信息量。
2. 原报告说「8–10 对、约 $25 能测清失败率差」，无依据：基础失败率只来自 3 个 run（精确 95% CI [0.01, 0.91]）。举例（假设库存 1/3 vs H1=0，单向 Fisher .05）：每臂 10 个 run 功效仅 0.43，15 个才 0.78（约 $45）；基础率更低时更差。任何扩跑的第一步应是先估库存失败率和本配置 AUC 噪声，不是为 H1 配功效。

**能说的（修订后）**：四个 run 都有效，预注册的停止规则未触发；H1 没有明显变差。**不能说的**：H1 改变了 AUC 或失败率，或自动改进方法有效。

详情：`docs/v3_pairs_results_20260921.md`。

## 失败机制分析（免费，用户建议先做而不扩跑）

对 v3_pairs 第一对那个失败 H0 run（job 1460465）做了完整链条追迹（`tools/trace_failure_lineage.py`，`docs/v3_failure_mechanism_20260921.md`）：

1. **字段首次使用**：节点 1 用了只在训练集里有的 `requester_user_flair`。
2. **报错被错误修复**：节点 1–3 因测试集缺该列报 `KeyError`；节点 4 的 debug 把缺列默认成 0 而不是删掉特征。
3. **验证失去意义**：节点 7 的 CV AUC 为 1.0，反馈 LLM 评价为"perfect"。
4. **污染谱系占据搜索**：16/25 个节点带该字段，5 个节点 val≥0.999，占 52% 搜索预算。
5. **测试预测塌缩**：节点 16、20 恰好是常数 0.0987，节点 7 范围仅 0.0007–0.0049；健康节点标准差 0.07–0.28。
6. **选中问题节点**：决策 LLM 选了节点 20（常数）而不是节点 14/15（0.653，诚实）；它的 prompt 里只有验证分/plan/findings，没代码、没预测分布。

**只有这一个 run 用了该字段**（其余 4 个 run 0 个节点提到）——是一次罕见分支，不是普遍现象。

> **下面这一条已被后续实测更正**：本文初版建议"打开 `AIDE_SUB_STATS` 让选择阶段看到预测画像"。实测发现该开关只能让画像进入**反馈 reviewer 的 prompt**，**进不了 submit 决策 prompt**（那里根本没有 term_out）。详见下一节。另外，关于"test 端 KeyError 就删特征"的初次建议也被用户纠正（见下面第 3 项决定）。

详情：`docs/v3_failure_mechanism_20260921.md`。

## 观测策略：用户三项决定的落地

针对失败机制分析的建议，用户做了三个决定，均已实现（commits 7bd43b5、a2b2471）：

**1. `AIDE_SUB_STATS` 纳入 harness 可配置范围**。新增 harness 文件 `observation_policy.json`：`{"submission_profile": true|false}`，严格 schema，缺省=stock，不影响旧版本哈希。开的话 runner 会设 `AIDE_SUB_STATS=1`，让 AIDE 将候选自己提交文件的数值画像（rows、n_unique、min/max/mean/std/NaN）追加到该节点的输出里。确认过该补丁只读候选自己的 `submission.csv`，从不读标签、分数或 grader。

该变量**不在**研究 repo 的调用方覆盖白名单里（没改那个 repo），所以改成**collect 时双向验证**：开了就要求运行配置写 `sub_stats = 1` 且节点被日志记录为已统计，否则 `delivery.ok=false` 并阻止 `improve`。**已在 CRC 上真实验证**（job 1460936，3 节点，$0.105）：配置行、日志、journal 三种信号都对上。

**2. 阶段化证据优先**。新模块 `rsi_mvp/stage_trace.py`：只要泄漏字段检测器定位过字段，collect 时（仅 train 任务）就重建链条并存进 `run_record.stage_trace`，进入 improver 的 `SUMMARY.md`/`runs.json`。每一步附上"决策 LLM 实际能看到什么"（在它的 submit prompt 日志上实测，不是假设）和未可见事实列表，让 improver 能区分"指令不够具体"与"信息从未展示"。

**3. 不把所有 test-side KeyError 都定义为删特征**。improver 指令改成：先确认特征在预测时是否可得，训练专有才删除并重验证，不得用测试端默认值掩盖（KeyError 也可能是列名/预处理不一致引发的）。

**实测发现一个需要更正的地方**：在那个烟测 run 上测了 78 条日志 prompt，只有 3 条（反馈 reviewer）包含画像，**0 条 submit 决策 prompt 包含它**（那里只有 id/stage/validation_metric/plan/findings）。所以开关要让决策方真正看到，得靠 reviewer 在 findings 里复述。reviewer 遇到 n_unique=1 这类退化画像会不会提到，**未测**。

详情：`docs/observation_policy_20260921.md`。

## 累计花费总账

按标价、无缓存折扣：

| 阶段 | 分项 | 小计 |
|---|---|---|
| Pilot（ROAP rule+agent 各 3 run） | rule $184.50 / agent $68.13 | $252.63 |
| v2（6 个 run） | | $148.88 |
| v3 烟测 + 真实 run + improver | $0.096 + $1.185 + $0.014 | $1.30 |
| 机制验证 improver（旧证据上真跑） | | $0.054 |
| v3_pairs（4 个 run） | 1.4296+1.6009+1.5254+1.4365 | $5.99 |
| 观测策略烟测 | | $0.105 |
| reviewer 重放 | 未提交（VPN/DNS 断开） | $0 |

**v3 系列小计（用户 09-20 设定的 $10 上限）：$7.45**。**项目大总：$408.96**。CRC 上当前没有任何 job。

## 未完成事项与下一步

**悬空中：reviewer 重放测试**。已写完并预注册 `tools/replay_reviewer.py`（commit 89bac52）：拿失败 run 里真实发给 reviewer 的 prompt，按补丁格式追加画像，用同一模型重新问，测它会不会在 summary 里提到预测退化；预估 $0.17，上限 $0.40。**因本机 VPN/DNS 断开无法解析 `crcfe01.crc.nd.edu`，尚未提交、未花任何钱**。

**其他未完成 / 未验证：**
- freeze / held-out（insults 任务）从未跑过。
- `AgentImprover` 处理带 hook 脚本的真实编辑（这次真模型没写 hook）未用真模型验证。
- 未推 GitHub（本机无 gh CLI，需用户给空 repo URL）。
- 共享 overlay 仍未应用 metric-guard 修复，共享 environment 仍带决策 bug。
- 未扫描 9 月 6 日后另外不带 harness bundle 的历史 agent run 是否同样回退过。

**成本参考**（若要扩跑 v3_pairs——未启动，等用户决定）：先估库存失败率和本配置 AUC 噪声比直接为 H1 配功效更值得；基于现有数据，每 run 约 $1–$2。

**文档索引**（均在 `real-rsi-mvp/docs/`）：
- `real_rsi_progress_20260920.md` — Pilot/v2 阶段（旧 rev 14 Claude 文档副本）
- `results_roap_h0_h1_h2.md` — Pilot 详细结果+订正
- `v3_single_agent_run_20260920.md` — 修 bug 后首次真实 agent run
- `v3_pairs_results_20260921.md` — 两对配对实验，含订正
- `v3_failure_mechanism_20260921.md` — 失败机制链条，含后续更正
- `observation_policy_20260921.md` — 观测策略三项决定

**关键代码**（`real-rsi-mvp/rsi_mvp/`）：`improver.py`（workspace/工具/AgentImprover）、`hooks.py`+`hook_runner.py`（沙箱）、`harness.py`（文件目录/验证/存储）、`stage_trace.py`（阶段化证据）。`tools/`：`cost_watchdog.py`、`build_agent_fix_overlay.sh`、`trace_failure_lineage.py`、`replay_reviewer.py`（未跑）。当前 193 个离线测试均通过（commit 89bac52）。
