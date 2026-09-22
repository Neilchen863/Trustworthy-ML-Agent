# Real RSI 进度总结（截至 2026-09-20）

> **已过期**：本文档只覆盖到 pilot/v2 阶段（09-20 用户叫停）。09-20 之后的 improver 重设计、v3 系列真实实验、失败机制分析、观测策略见
> `real_rsi_progress_20260922.md`（也是最新的 Claude 文档），这里的内容不会再更新。
>
> 本文件是 Claude 文档《Real RSI 进度总结（截至 2026-09-20）》（rev 14）的 markdown 副本。文档里的日期/提及芯片在此为普通文本。
> 以文档为准；如两者不一致，说明文档之后又被编辑过。
> 它取代旧文档《Real RSI MVP：实现与 ROAP H0→H1→H2 实验报告》（该文档已加"已过期"提示）。

## 一页结论

Real RSI 的工程部分已经做完并在 CRC 上真实跑通，但**到目前没有一个可以信的实验结论**：两次实验都被一个管线 bug 污染，第二次（v2）在发现它后被你叫停，目前 CRC 上没有任何 job 在跑。

- **实现**：嵌在大 repo 里的独立 git repo `real-rsi-mvp/`，135 个离线测试通过，不改大 repo 任何被跟踪文件。
- **实验**：pilot（ROAP，rule 与 agent 各 3 个 run）全部跑完；v2（每格 3 个 replicate，计划 18 个 run）只跑了 6 个，其中 2 个跑完、4 个被杀。
- **花费**：约 **$401.6**（按标价、无缓存折扣，上限），pilot $252.63，v2 $148.88。
- **最重要的发现**（都在你的研究仓库里，均未修复）：
  1. **agent 模式从未真正运行**：决策 LLM 调用每次都在发出前报错并回退成 rule 策略，所以 pilot 和 v2 里所有"agent"结论都不成立。
  2. **metric-guard 把 `5-fold` 里的 5 当成指标值**，制造出 val=5.0 的幻影节点；我写了补丁且在真实 job 里验证过，但只应用到了专用 overlay。
  3. **扫描器每种模式只保留一个示例 finding**，我一度把它当成命中节点数，算错了 mirage 占比（已订正）。
- **能站得住的东西**：管线端到端可用、rule 模式不受上述 agent bug 影响、防污染守卫有效、每个发现都有可复现的证据。
- **没有做的**：freeze、held-out、修复 agent 决策 bug、推送 GitHub。

## 时间线

整件事从读代码到 v2 被叫停，共两天。下表中的时间都是 CRC 日志上的 UTC，实现阶段只写日期。

| 时间 | 事件 |
| --- | --- |
| 09-19 | 读大 repo（AIDE rule / agent 模式的实现），读 spec v0.3，向你确认 4 个设计决定 |
| 09-19 | 实现 MVP（任务包、事件、verifier、记忆、meta-improver、harness 版本化、防污染守卫、CRC wrapper），在真实归档 run 上冒烟并修掉首批 bug |
| 09-19 | 发现 CRC 上 `env.sh` 的 key 已失效，你自己执行了修复脚本 |
| 09-19 14:23 | pilot round 0 提交（rule `1457836`、agent `1457837`），常驻 driver 随后启动 |
| 09-20 00:06 | pilot 全部 6 个 run 收集完，driver 报 done |
| 09-20 | 深查 pilot：发现 val=5.0 是 metric-guard 的 bug；随后发现我把 mirage 占比算错了并订正 |
| 09-20 | 代码改进：每节点占比 reward、记忆与补丁分离、replicates + pin first draft、写好 guard 补丁 |
| 09-20 ≈ 05:23 | 专用 overlay 建好并通过 smoke（探针 `1459599` 记录 0.6323，共享 overlay 对照 `1459600` 记录 5.0） |
| 09-20 05:24 | v2 启动：6 个 job（2 模式 × 3 replicate）同时提交 |
| 09-20 ≈ 07:00 | 你问费用；查成本时发现 agent 模式的决策 LLM 从未成功调用过 |
| 09-20 ≈ 07:50 | 你叫停：全部 v2 job 被删除，一个误启动的 driver 也被停掉（没做任何事） |
| 09-20 | 把 CRC 上的东西拉回本地，并写本文档 |

## 已实现的系统

`real-rsi-mvp/` 是一层包在现有 AIDE 外面的薄流水线：每一轮跑一次现有模式，事后从运行目录读出轨迹、打分、存记忆，再让 LLM 提出一个有界的 harness 补丁。可变对象只有 harness，AIDE 本体、任务环境和评测器都不动。135 个离线测试在本地通过（CRC 的 Python 3.9 上也跑过）。

| 部分 | 做法 |
| --- | --- |
| 任务包 | `tasks/<name>/`：`task.yaml`、`aide_config.yaml`、`verifier_config.yaml`；`random_acts_of_pizza`（train）和 `insults_heldout`（test，只有配置） |
| harness 如何送入 AIDE | 全部走现有通道：`PROMPT_VARIANT` notes 文件（内容寻址、不可变）、`AIDE_SELECTION_MODE`、`AIDE_MAX_STAGNATION` 等环境变量；不改大 repo 任何被跟踪文件，已在 CRC 上逐一核对变量在调用方覆盖白名单里 |
| 决策事件 | 从 `journal.json` 与 `aide.log` 事后重建，两种模式同一 schema；rationale 标注 logged / inferred |
| verifier bank | 6/7 个复用 vendored 的 `audit_run.py`（字节一致、sha256 有测试守着）；`validation_mirage` 现在按**节点占比区间 [下界, 上界]** 计 reward，其余仍按整个 run 的严重度；`decision_insensitive_prompting` 未实现 |
| 记忆 | 每个可操作的 verifier 命中一条记录；新 harness 默认**不**把 lesson 渲染进 prompt，只给 meta-improver |
| meta-improver | 真实 gpt-4o，一次只能提一个有界补丁（prompt / decision_policy / memory_policy）；看到占比区间而不是中点 |
| 防污染守卫 | 只有 train 任务的 official grade 能给 meta-improver，每次暴露写入账本；test 任务在 `freeze` 前不能提交；freeze 后训练即结束 |
| driver | 可断点续跑；一轮 = 同一 harness 的 K 个 replicate 同时提交、收齐后只做一次 improve；遇到无评分、harness 未送达、补丁无效等异常会停机 |
| 重复与 first draft | `replicates: [1,2,3]` + 中性无泄漏 seed（本地真实数据 5 折 AUC 0.6364），用 AIDE 现有的 seed-node 机制送入，并检查送达 |
| 专用 overlay | `--overlay-path` 让一次实验挂自己的 agent-fix overlay，不影响共享的 |
| 工具 | `rsi drive / summarize / freeze / heldout-report`、`tools/smoke_overlay.py`、`patches/`（未应用的 guard 补丁） |

## Pilot 实验（2026-09-19）

Pilot 在 ROAP 上跑了 rule 和 agent 各 3 个 run（H0→H1→H2，每格 1 个，4 小时 / 500 步 / gpt-4o）；**只有 rule 这一臂的数据是有效的**，agent 这一臂实际上也是 rule 策略（见"发现的问题"）。

| mode | harness | 官方 AUC | 提交节点验证（记录→真实） | 节点 | mirage 命中占比（下界～上界） |
| --- | --- | --- | --- | --- | --- |
| rule | H0 | 0.62227 | 0.6996 | 500 | 5.0%～6.2% |
| rule | H1 | 0.57471 | 0.7483 | 500 | 2.8% |
| rule | H2 | 0.62800 | 0.6901 | 500 | 0.4%～0.8% |
| agent* | H0 | 0.62724 | 5.0 → 0.6249 | 466 | 11.8%～24.2% |
| agent* | H1 | 0.62051 | 0.7018 | 66 | 9.1%～13.6% |
| agent* | H2 | 0.62411 | 5.0 → 0.6323 | 500 | 10.0%～26.6% |

\* agent 行实际是 rule 策略，只能当作"rule + 不同 notes"的额外样本，不能读成 agent 模式的结果。

**6 个补丁**：全部是真实 gpt-4o 提出、通过有界校验的文字追加，内容都围绕"预处理只在训练折内 / 切分之后做、避开预测时不可用的字段"，没有一个涉及"怎样选提交节点"。补丁与自动注入的记忆 lesson 几乎是同一句话，所以补丁效果和记忆效果分不开（v2 已分开）。

**能说的**：

- 端到端管线能无人值守地跑通，补丁文字确实进了每一次 LLM 调用（原始 prompt 日志里每个节点出现 2 次）。
- rule 一臂的 mirage 命中占比下界从 5.0% 降到 2.8% 再到 0.4%，而官方分数 0.622 / 0.575 / 0.628 没有趋势。

**不能说的**：任何 harness 优于另一个（每格只有 1 个 run，官方分数波动 0.575–0.628）；任何关于 agent 模式的结论；"rule 线的下降是补丁的功劳"——只是一个提示，需要重复才能判断。

每个 run 的搜索量也不可比（节点数 66–500，候选执行时间中位数 1–30 秒），原因是首个 draft 选了不同重量的模型家族，而不是补丁造成的——这正是 v2 要用 pin first draft 去控制的东西。

## v2 实验（2026-09-20，已叫停）

v2 计划跑 18 个 run（rule / agent 各 3 个 replicate × H0→H1→H2），只启动了第一批 6 个，其中 2 个跑完、4 个被杀；由于 agent 一臂无效且你叫停全部，**没有产生任何可用于比较 harness 的数据**。

**相比 pilot 改了什么**：mirage reward 改为节点占比区间；记忆与补丁分开；每格 3 个 replicate 并 pin 同一个中性 first draft；同一轮收齐 replicates 后才做一次 improve；新 state root `experiments/v2`（不继承旧 H1/H2/记忆）；并用**专用 overlay** 应用 metric-guard 补丁（共享 overlay 与共享 scripts 未动）。

**smoke（全部通过）**：

| 检查 | 结果 |
| --- | --- |
| 专用 overlay 与共享 overlay 的内容差异 | 列出 `aide` 包下 44 个文件做 diff，**恰好只有 `_metric_parser.py` 不同** |
| 静态解析 8 条文本 | 专用：全部正确；共享（对照）：6 条仍读成 5.0，`10-fold` 读成 10.0 |
| 真实 job 探针 | 专用 overlay `1459599` 记录 **0.6323**；共享 overlay `1459600` 记录 **5.0**；两个 job 日志里挂载的 overlay 路径各自对得上 |
| 6 个正式 run | 日志均显示挂载专用 overlay；`run_config` 均显示同一个 seed（`rsi_cd41d76ba2`）；读得出来的 5 个 run 第 1 个节点验证值都是 0.636461 |

**6 个 job 的去向**：

| job | 模式 | 结局 | 官方 AUC | mirage reward | 花费 |
| --- | --- | --- | --- | --- | --- |
| 1459602 | rule rep1 | 跑完、已收集 | 0.6687 | -0.014 | $43.98 |
| 1459603 | rule rep2 | 被杀 | — | — | $43.29 |
| 1459604 | rule rep3 | 被杀 | — | — | $20.06 |
| 1459605 | agent rep1 | 被杀 | — | — | $1.02 |
| 1459606 | agent rep2 | 跑完、已收集（无效） | 0.6406 | -0.108 | $38.55 |
| 1459607 | agent rep3 | 被杀 | — | — | $1.98 |

**有效的东西只有三样**：

- pin first draft 在真实 run 里生效（第 1 个节点逐位相同）；
- 专用 overlay 里的补丁在真实 job 里生效（所有读得到的 run 里没有任何 val>1.0 的节点，pilot 里 agent 线很快就出现 val=5.0）；
- 一个有效的 rule 样本（1459602），但单个样本什么也说明不了。

**为什么停**：查成本时发现 agent 模式的决策 LLM 从未成功调用，我建议"停 agent 臂、修好后重跑"，你的决定是杀掉后不再跑，并明确要求 rule 也不跑。

## 发现的问题与根因

前两条是你的研究仓库里会改变实验结论的真 bug，只有第 2 条有可应用的补丁，且未应用到共享环境；第 3 条是我自己的分析错误，已订正。

| 问题与影响 | 证据 | 状态 |
| --- | --- | --- |
| **1. agent 模式从未真正运行。** `_inject_agent_decision.py::_ordered_selection_prompt` 在无 harness bundle 的默认分支把 `[(标题, 内容), …]` 元组列表直接塞进 prompt，后端对列表元素调用 `.strip()` 报错，决策 LLM 在发出前就失败并回退成 rule 策略。影响 pilot 与 v2 所有 agent 结论，可能还有你 9 月 6 日之后其他不带 harness bundle 的 agent 实验（未扫描） | `LLM chose` 计数 0；搜索决策 1060 次失败、提交选择 560 次失败；每节点正好 2 次调用（与 rule 一样）；用已安装模块复现出 `AttributeError: 'tuple' object has no attribute 'strip'`；7 月旧 agent run 正常 | **未修**。修法：把默认分支展平成 `title -> content` 字典，一个函数 |
| **2. metric-guard 把 `5-fold` 里的 5 当成指标值。** 对 CV 汇总行取"最后一个指标名之后的第一个数字"，反馈 LLM 读对的 0.6323 被纠成 5.0，劫持 argmax。pilot agent 线 H0/H2 分别有 11 / 16 个节点被改坏；历史上经过该守卫的 run 可能都有同类 artifact（未扫描） | 真实解析器复现；节点自己的日志 `value=5.0 … corrected_reviewer_value=0.6323`；真实 job 对照：专用 overlay 0.6323、共享 overlay 5.0 | 补丁已写、已验证（仓库自带测试 + 回归测试），只应用在专用 overlay；**共享 overlay 仍有 bug**。运行中的 guard 是 overlay 里的拷贝，只改 `scripts/` 不生效 |
| **3. 扫描器每种（检测器，模式）每个 run 只保留一个示例 finding**，真实命中数在 message 的 `[共 N 个节点命中此模式]` 里。我曾把 finding 数当作命中节点数，把 mirage 占比低估了 10 倍以上 | 用真实计数重算 6 个 run；测试里 3 个含同一模式的节点只产生 1 个 finding | 已订正，reward 改为基于真实计数的下界/上界 |
| 4. 真实 journal 的每个 `node.parent` 都是空，父子关系只在 `node2parent`；扫描器因此把"污染谱系后代"写成 0（真实 486/500） | agent 日志对齐率 9/110 → 110/110；构造数据掩盖了它 | MVP 已修（适配层归一化）；研究仓库的 `audit_run.py` 未修 |
| 5. 我把 `E0_coverage_gap` 路由到"不属于任何 verifier"，"没法检查"会显示成奇怪的 0 | 每个 run 6–10 个节点终端输出被过滤，部分由 verbose log 兜底 | 未改，影响轻微 |
| 6. 共享 overlay 整个文件的 md5 会因每次挂载（包括只读）而变 | 同一内容的前后 md5 不同，内部 44 个文件不变 | 方法注意事项：证明"没动"要比内容，不能比整个文件 md5 |
| 7. 我自己的代码/操作缺陷 | driver 提前停机写不出停机文件、最后一轮不检查送达、freeze 后仍能 improve；监视器 `pgrep` 自匹配；`runs_index` 漏记 overlay/seed；拉取脚本第 6 步静默失败却报 exit=0；把"杀 job"和"重启 driver"绑在一条命令里导致一个 driver 被误启动 | 均已修或已发现并停掉（误启动的 driver 没做任何事） |

## 已作废或修正的说法

下表中的说法都曾出现在我之前的汇报或第一份报告文档里，现在一律以本文为准。

| 早期说法 | 现在的事实 |
| --- | --- |
| "补丁没有改变被检测到的行为" | 作废。数据既不能证明也不能排除效果（rule 线下界 5.0%→2.8%→0.4%，但每格只有 1 个 run） |
| "validation_mirage 饱和，每个 run 只有 0.2–0.4% 的节点命中" | 算错（数了示例 finding，没数真实命中数）。真实占比 rule 0.4%–6.2%、agent 9%–27%，行为在 agent 下很常见 |
| "agent 线的 anchoring = -1 是 agent 的决策失误" | 主要是 5.0 幻影节点；而且 agent 的决策根本没运行，提交只是 rule 的贪心 argmax |
| "agent H1 的 anchoring 从 -1 变成 0 是补丁的效果" | 不是：那一轮只是 guard 没触发；agent 也并不存在 |
| "decision_policy 补丁影响了 agent 的选择" | 决策 agent 没有运行，这些文字只进了代码生成的 prompt |
| "pilot 里 rule 与 agent 的对比" | 没有意义：两者都是 rule 策略，只是 notes 不同 |
| "只改 `scripts/_metric_parser_runtime.py` 就能修好 guard" | 不够：运行中的 guard 是 overlay 里的拷贝 |
| "重新执行 08 补丁即可" | 会把反馈接口的其他代码一起升级；所以专用 overlay 里只替换 `_metric_parser.py` 一个文件 |
| "用整个文件的 md5 可以证明共享 overlay 没被改" | 不行，挂载会改变它；要比内部文件内容 |

**第一份报告文档《Real RSI MVP：实现与 ROAP H0→H1→H2 实验报告》已过期**：它的摘要、实验结果、深查、局限和未决事项中关于 agent 模式的内容都不再成立，也没有 v2、花费和 agent 决策 bug。我已在那份文档顶部加了"已过期"提示并指向本文，没有改其他内容。

## 花费

到目前为止的 OpenAI API 花费约 **$401.6**，其中约 $110 花在了实际上是 rule 策略的"agent"一臂。以下按 gpt-4o-2024-08-06 标价（每百万 token 输入 $2.5、输出 $10）计算，没有扣除缓存折扣，所以是上限。

| 项目 | 花费 |
| --- | --- |
| pilot（6 个 run） | $252.63（rule $184.50，agent $68.13） |
| v2（6 个 run，2 个跑完、4 个被杀） | $148.88（rule $107.33，agent $41.55） |
| smoke 探针（2 个极小 job） | ≈ $0.02 |
| meta-improver（4 次真实调用，估算） | ≈ $0.03 |
| **合计** | **≈ $401.6** |

口径说明：

- token 数来自每个 run 的 `logs/token_usage.jsonl`。agent 模式的决策调用在发出前就失败了，没有产生费用，所以这个日志是完整的，没有漏计。
- meta-improver 走 urllib，不在 token 日志里，按保存下来的输入长度估算。v2 没有跑到 improve，所以 v2 里没有 meta-improver 费用。
- 不包含 Claude 本身的使用量（我看不到），也不包含 CRC 计算资源。
- 单个 500 步的 rule run 约 $44–65；如果当初的 18 个 run 都跑，按 pilot 平均每个约 $42 估，总共约 $600–800。

## 资产位置

CRC 上与 real_rsi 相关的东西已经全部拉回本地并校验（14 个 run 目录逐文件与 CRC 一致，专用 overlay 的 md5 与 CRC 一致），只有密钥类文件、虚拟环境和你的共享环境没拉。

| 内容 | 本地位置 |
| --- | --- |
| MVP 代码、测试（135 个）、任务包、vendored 扫描器、补丁、工具、文档 | `MLE-bench_AIDE/real-rsi-mvp/`（独立 git，main；通过大 repo 的 `.git/info/exclude` 隐藏） |
| pilot 的 harness 版本（H0–H2）、记忆、轮次记录、暴露账本 | `real-rsi-mvp/harness_versions/`、`real-rsi-mvp/rounds/`（6 个版本 tag） |
| v2 的状态（H0、两个已收集的 run 记录、driver 日志、来源说明） | `real-rsi-mvp/experiments/v2/` |
| 14 个 run 目录（pilot 6、smoke 2、v2 6），共约 2.2GB | `MLE-bench_AIDE/runs/random-acts-of-pizza/`（大 repo 的 gitignored 目录，与已有约定一致） |
| 专用 overlay（1.0GiB）与私有打过补丁的 scripts 副本 | `real-rsi-mvp/artifacts/crc/overlays/v2/` |
| 14 个 job 的标准输出（里面有"挂载了哪个 overlay"那一行） | `real-rsi-mvp/artifacts/crc/sge_out/` |
| 运行时写进研究仓库 CRC 副本的 4 个 notes 文件和 seed 文件 | `real-rsi-mvp/artifacts/crc/staged/` |
| 本文档的 markdown 副本 | `real-rsi-mvp/docs/real_rsi_progress_20260920.md` |

`artifacts/` 已在 `.gitignore` 里（我实际用 `git check-ignore` 验证过，因为一开始我写的忽略行带了行尾注释，是无效的），不会被提交。

**没有拉回的**：

- 密钥类文件：`~/mlebench-aide/config/env.sh`、它的备份 `env.sh.bak.rsi-20260919`（里面是旧的失效 key）、`autoML-pilot/config/run.env`。
- CRC 上的虚拟环境 `~/real-rsi-mvp/.venv`（可以重建）。
- 你的共享环境：镜像 `.sif`（约 11GB）、共享 overlay，以及研究仓库其他实验的 run。

**CRC 上仍然存在的**：`~/real-rsi-mvp`（代码、状态、`.venv`、专用 overlay）和 `~/mlebench-aide/runs/random-acts-of-pizza/` 下的同一批 run 目录，没有删。`/users` 剩约 11G。

**校验方式**：逐目录用 rsync 干跑对比，没有任何差异才算一致；overlay 比对两边的 md5。这次拉取时有个教训：我的拉取脚本在第 6 步（overlay）因为 macOS 自带的 rsync 2.6.9 不认识一个参数而失败，却仍报了 `exit=0`，是校验发现本地 overlay 目录是 0B 才看出来。已去掉该参数重拉并校验通过。

## 开放问题与可选的下一步

要继续做有意义的实验，建议的顺序是先修 agent 决策 bug 并验证，再决定重跑的范围。下面这些都需要你来决定，我没有做任何一项。

| 事项 | 选项 | 成本或风险 |
| --- | --- | --- |
| 修 agent 决策 bug | 把 `_ordered_selection_prompt` 的默认分支展平成 `title -> content`；可只放进专用 overlay，也可以修研究仓库本体。验收标准：真实 job 里出现 `LLM chose` 而不是 `failed` | 改的是你的仓库或 overlay；修之前任何 agent 模式实验都不该跑 |
| 扫描历史 agent run | 统计 9 月 6 日之后不带 harness bundle 的 agent run 里 `failed ... falling back` 的比例 | 只读，成本很低，可以知道你另外的实验受没受影响 |
| guard 补丁是否应用到共享 overlay | 先备份 1GB overlay，再只替换 `_metric_parser.py`（已在专用 overlay 上验证过这个做法） | 影响你所有其他实验，但不修的话它们都会有 5.0 类幻影 |
| 是否重跑 v2 | ① 只跑 rule 一臂（9 个 run，约 $350–450）；② 修好 agent 后两臂都跑（约 $600–800）；③ 不再跑 | ① 不需要修任何东西就能开始，但只回答 rule 的问题；你的最新指示是不再跑 |
| 清理 CRC | 专用 overlay 1.1GB、旧 run 目录约 2.2GB（已拉回本地）、`/users` 只剩约 11G | 不可逆，建议先确认本地拉回完整再删 |
| GitHub 推送 | 提供一个空 repo 的 URL | 本机没有 `gh`，确认后才推送；推之前需要确认 `docs/` 里的旧结论已更新 |
| freeze 与 held-out | 等有值得冻结的 H_final 再做，并为 insults 补中性 seed | 当前没有任何有效的 harness 改进证据，现在冻结没意义 |
| 仓库里的结果文档 | `docs/results_roap_h0_h1_h2.md` 已在顶部加了 agent 无效的警示并补了 v2 结局 | 已更新 |
