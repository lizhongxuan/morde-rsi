# RRSI → morde-rsi 对照

本文档记录 morde-rsi 从 RRSI（`google-research/rrsi`，Apache 2.0，参考副本在
`refs/rrsi/`，已加入 `.gitignore`，不随本仓库发布）借了什么、改了什么。
morde-rsi **没有整体复制 RRSI 代码**：核心模块是按 RRSI 的算法重写的轻量版，
只借结构与公式；凡直接沿用的公式都在代码注释里注明来源文件。

RRSI 参考版本：2026-09-21 论文发布后的 main（`git clone --depth 1`，
2026-11-13 拉取）。RRSI 论文：arXiv:2609.24972。

## 模块对照

| RRSI 模块（来源文件） | morde-rsi 模块 | 借了什么 | 改了什么 |
|---|---|---|---|
| `rrsi/history.py` — 每 edit 一条 JSONL 记录；`L_t`、`T_t`、`g_t`、`B_t`、stall flag、exploration 指令 | `rsi/history.py` | 一 edit（此处为一候选）一条 JSONL；`ts` 自动补；`records()` / `append()` / 摘要函数 | 记录单位从"harness 的一次 edit"改为"修一个 bug 的一个候选"；字段改为 bug 修复语义：`task_id`、`candidate_id`、`diff_sha256`、三层结果、`verdict`、`baseline_commit`、成本；`frontier()` 返回当前基线（commit + 分数），而不是 harness 分支 |
| `rrsi/critic.py` — 评估前防泄漏检查：确定性 precheck（通用黑名单 + domain patterns）+ LLM 评审 | `rsi/critic.py` | 两层结构、precheck 先行、空 diff 拒绝、"评估前先审 diff，审不过不花评估成本" | **删掉了 LLM 评审**：morde-rsi 本地优先、不调外部模型，critic 只做确定性检查（路径黑名单 + 凭据正则 + 留出任务 id）；LLM 评审的那一层由"窄白名单 + 人工审 PR"替代。`review()` 返回 `gaming` 标志，命中受保护路径即 `GAMING-DETECTED` |
| `rrsi/selection.py` — 算法 2：噪声下限 `S' >= S* - delta`、成本规则（`beta0 + beta1 * dS` / 带内加权）、argmax | `rsi/selection.py` | 算法 2 的完整形状：`cost_rule`、`judge`、`select_round`、`Decision`；`relative_cost_change` | 加了一道**硬门**：F2P（复现测试）必须全过、P2P（回归）零退化，否则直接不可采纳（RRSI 只有分数门，没有这道语义门）。`S` 的定义改了：RRSI 的 S 是基准任务通过率；morde-rsi 的 S 是任务检查的加权通过率（F2P 权重 2、P2P 权重 1、held-out 权重 2），由 `eval_from_reward()` 产出。**P2P 是逐测试的**（见下"基线发现"）：`test_map()` 从 `go test -json` 的逐测试结果里取基线通过集，候选必须保持这些测试通过；基线已失败的测试不计入 P2P。2026-11-13 新增 `classify_p2p_failure()`：单点 P2P 失败走 NEEDS_HUMAN（见下"反馈机制"），不是 RRSI 的东西 |
| —（RRSI 没有对应模块；反馈是 Morde 的需求） | `rsi/feedback.py` | — | **新加**：1 层静默遥测、2 层一键审批卡、3 层深度评审（桩）、三票裁决、反馈转学习、Fixer 可见反馈的脱敏。详见下"反馈机制" |
| `rrsi/schedule.py` — 退火编辑预算 `b_t = ceil(b_min + (b_max-b_min)/2 * (1+cos(pi t/T)))` | `rsi/schedule.py` | 公式原样 | 未改。试点期 `b_max` 取 2（RRSI coding 取 4），见 `domains/morde/rrsi.json` |
| `rrsi/propose.py` — 提议者：严格 JSON 动作协议、done() 校验预算与标签 | `rsi/fixer.py`（接口）+ `domains/morde/SKILL.md`（提议者宪法） | done() 的校验思想：预算、标签、"超预算或无标签的提交直接打回" | 提议者本身不是本项目的代码：Fixer 是 DSH Task（见 `DSHFixer` 桩），提议规则写进 `SKILL.md`/`PATTERNS.md` 当 DSH Task 的任务书。`ManualFixer` 是试点用的人工模式，账本里明确标注 `fixer="manual-pilot"` |
| `rrsi/evaluate.py` — `S_hat`/`C_hat` 聚合；缺失 trial 计 0 不缺位 | `rsi/selection.py` 的 `EvalResult`/`aggregate` + `rsi/verifier.py` 的 `Reward` | 聚合公式、"缺失计 0、分母不变"（候选不能靠毁掉难 trial 来显得更好） | 评估对象从"harness 在基准任务上的 trial"改为"候选 diff 在三层检查上的结果"；`TaskResult` 的 trial 改为每组检查（f2p / p2p / heldout） |
| `rrsi.py` — CLI：`baseline` / `calibrate` / `round` / `run` / `readjudicate` / `reevaluate` / `heldout` / `smoke` / `status` | `rsi.py`（本仓库根） | 子命令形状、`--domain` + 超参数覆盖 | 砍掉 `calibrate` / `readjudicate` / `reevaluate` / `heldout`（试点期不需要）；保留 `smoke` / `baseline` / `round` / `run` / `status`；domain 固定为 morde |
| `rrsi/domain.py` — Domain 抽象（`evolve_ids` / `heldout_ids` / `run` / `score` / `smoke` / `critic_patterns` / `guards`） | `domains/morde/adapter.py` | Domain 适配器接口形状 | 评估后端从 harbor + Docker 容器改为本机 git worktree + 确定性验证器脚本；`run()` 直接调 `rsi/verifier.py`；`smoke()` 只做环境存活检查（go/node/验证器脚本存在） |
| `rrsi/gitops.py` + `rrsi/loop.py` — worktree 管理、候选分支、fast-forward 采纳 | `rsi/worktree.py` + `rsi/controller.py` | "每个候选一个 worktree，采纳即 fast-forward，基线永远是一个 commit" | 采纳不 fast-forward 到主分支：候选只提交到 `rsi/<task-id>` 分支，**永不自动合并**；合入主分支是人工审 PR 的事。`loop.py` 的 605 行大循环拆成 `controller.py` 的小状态机 |
| `rrsi/config.py` — `RRSIConfig`（超参数 + CLI 覆盖） | `rsi/config.py` | 同名配置类 + JSON 加载 | 字段改为 morde 试点值；新增 `max_submissions`、`token_budget`、`time_budget_s`、`timeout_s` |
| `domains/coding/rrsi.json` | `domains/morde/rrsi.json` | 全部超参数键名（T/k/m/b_min/b_max/w/m_draft/delta/beta0/beta1/w_s/w_c/w_n/n_prune） | 取值改为试点值：T=5、k=1、m=1、b_max=2、delta=0.02（见下）；删掉 harbor/Vertex AI/dataset 相关键；新增预算与 fixer 配置 |
| `domains/coding/SKILL.md` + `PATTERNS.md` | `domains/morde/SKILL.md` + `domains/morde/PATTERNS.md` | "提议者宪法 + 模式库（参考，不是白名单）"的写法 | 内容全部重写为 Morde 语义：白名单路径、只做 A 级小修、绝不碰测试/验证器/鉴权/审批/事件链 |
| `domains/coding/README.md` | `domains/morde/README.md` | domain 说明文档的写法 | 内容改为 Morde：任务 JSON 怎么填、怎么跑、环境要求 |
| `domains/coding/adapter.py` — `CodingDomain` | `domains/morde/adapter.py` — `MordeDomain` | `critic_patterns` 的写法（domain 正则黑名单）、`score()` 的 `TaskResult` 结构 | 见上表 `domain.py` 一行 |

## 没有借的东西

- `rrsi/llm.py`、`analyst.py`、`digester.py`：RRSI 的 LLM 调用层与分析器。
  morde-rsi 不调任何外部模型，分析器那一层在试点期由人做（看账本 + 看
  reward.json），以后再说。
- `rrsi/loop.py` 的并行评估、`evaluate.py` 的 `k` 次 trial：试点期 k=1、串行。
- harbor / Docker / Terminal-Bench / SWE-bench：RRSI coding 实例的评估后端，
  Morde 用自己的 go test + vitest + Playwright。
- `rrsi/calibrate.py`：噪声带重估计。试点期 delta 固定 0.02（见下），不重估计。

## delta = 0.02 的依据

RRSI coding 实例的 delta 是 0.017（178 次 trial 里 3 次通过），由 `calibrate`
从基线评估的 bootstrap 得来。morde-rsi 试点期每个任务的检查数很少（约
10–20 个检查点），3 个检查点 ≈ 0.02 左右。**这是一个试点期拍脑袋的初值，
不是测出来的**：第一个真实试点跑完后，用基线重复测量的方差重估计，
再写进 `domains/morde/rrsi.json`。`selection.py` 的 `delta` 是可配参数，
改配置即可，不用改代码。

## Morde 验证命令（已验证 Go 侧；Portal 侧未验证）

2026-11-13 在 `/tmp/morde-verify-clone`（`~/workspace/morde-check` 的干净克隆，
HEAD 273d300；**原仓库未动任何文件**）上跑通：

| 层 | 命令（来源） | 实测结果 |
|---|---|---|
| A | `cd server && go build ./...`（`server/Makefile: build`） | 通过，首次 43 秒（含下载），缓存后 4.9 秒 |
| A | `cd server && go vet ./...`（分包跑 `go vet ./internal/app/ ./internal/worker/`） | 通过，1.3 秒。注：全仓 `go vet ./...` 会报一个既有问题（`internal/storage/workspacefs` 的测试 import cycle），但分包 vet 通过 |
| A | `cd server && go test ./internal/worker/ -run TestTaskWorker -count=1` | 通过，0.245 秒 |
| B | `cd server && go test -json ./internal/worker/ -count=1`（`mode: suite-ran`） | 套件跑完，345 个测试结果被解析；**46 个测试在基线就失败**（见下） |
| C | `cd server && go test -json ./internal/app/ -count=1` | 套件跑完，1328 个测试结果被解析（含子测试）；**1 个测试在基线就失败** |

真实 `scripts/verify.py` 对克隆跑了两轮（任务 MORDE-SMOKE-001/002，
`/tmp/morde-smoke-task.json`、`/tmp/morde-smoke-task2.json`，证据在
`/tmp/evidence/`，reward 在 `/tmp/morde-reward.json`、`/tmp/morde-reward2.json`）：
第一轮 tier A 全过、tier B/C 整包失败；第二轮（逐测试 tier B）tier B
"suite ran" 通过、345 个测试结果入账，score 0.8。

### 基线发现：Morde 基线本身就有环境依赖的失败测试

- worker 包 46 个失败：43 个快速失败全是 `account Agent Runtime handshake
  is invalid`（缺 DSH 运行时握手），3 个浏览器测试超时（20–45 秒，
  需真实浏览器/DSH browser 工具环境）。
- app 包 1 个失败：`TestQuotaSettlePersistsOwnerStoragePauseBeforeAborting`，
  `workspace current path is not a safe regular file or directory`（workspace
  路径假设，环境相关）。
- 结论：这些失败与产品 bug 无关，是测试环境缺 DSH 运行时/浏览器/路径假设。
  **因此 tier B 绝不能是"整包必须通过"**：基线就过不了，任何候选都过不了。
  改为逐测试 P2P（SWE-bench 语义）：基线通过的测试修完还得通过；
  基线已失败的测试不计入 P2P。实现在 `rsi/verifier.py`
  （`go-test-json` 解析 + tier `mode: suite-ran`）和 `rsi/selection.py`
  （`test_map()` + `eval_from_reward()` 的覆盖参数）里；基线跑全量
  （`fail_fast=False`）以捕获完整的逐测试基线图。
- tier B 的范围建议：只跑受影响包 + 依赖包，不要盲目 `go test ./...`
  （worker 单包就要 166 秒；全仓更久）。任务 JSON 里写具体包。

**Portal 侧已在第一轮试点验证（2026-11-13，CAND-001）**：Judge worktree 里
`npm ci` 实装依赖后，`npm run typecheck` + `npx vitest run` 全过
（51 文件 / 529 测试，基线）。`playwright test` 仍未验证。

## Morde 仓库约定（`~/workspace/morde-check/AGENTS.md`）

- DSH 是 Morde 唯一的 Agent Runtime，不另起第二套。morde-rsi 遵守这一点：
  Fixer 是 DSH Task（在 Morde 之外、独立项目里调用），验证器是确定性脚本，
  不是 Agent；RSI 不往 Morde 里塞任何 runtime。
- DSH 插件接入要遵守用户/会话/任务隔离与授权、审批、凭据边界。morde-rsi
  的 Fixer 工作区与 Morde 运行环境隔离，只改 git worktree 里的代码。

## 试点第一轮 CAND-001 发现的两个流程缺口（2026-11-13）

第一轮试点（CAND-001，routingStatusText 缺 routed 分支）跑之前发现骨架有
两个缺口，都是最小改动补上，测试已覆盖：

### 缺口 1：tier A 复现测试在仓库里不存在，验证器没处跑

骨架设计假定 tier A 的复现测试已在仓库里（像 fixture 的 test_calc.py
那样已提交）。CAND-001 的复现测试 `src/app/message-status.test.ts`
是新建的，仓库里没有；Fixer 又不许碰测试文件（critic 会判
GAMING-DETECTED），所以复现测试不能靠 Fixer 带进来，验证器必须自己注入。

补丁（`rsi/verifier.py`）：tier A 增加 `repro: {src, dest}` 注入，与 tier C
的 held-out 注入共用 `_inject_files()`：tier A 跑之前从工作区之外把复现
测试拷进 worktree，跑完立即删除。效果：Fixer 永远看不到复现测试文件；
tier B 只量已有测试。测试：`tests/test_verifier.py` 新增
`test_tier_a_repro_injected_then_removed` /
`test_tier_a_repro_passes_after_fix`（49/49 通过）。

### 缺口 2：worktree 里没有 node_modules，tsc/vitest 跑不起来

`git worktree add` 只检出受版本控制的文件；`node_modules` 是 gitignored
的，所以全新 worktree 里没有它，`npm run typecheck` / `npx vitest` 会失败。

第一次补丁（软链）是错的：`rsi/worktree.py::link_node_modules` 把主检出的
node_modules 软链进 worktree，结果基线 tier B 的 2 个文件失败
（`src/App.test.tsx`、`src/routes/Assistant.test.tsx`）：Vite 的 module
runner 拒绝真实路径在项目根之外的文件——pdfjs worker 的 `?url` 导入解析
到软链目标（主检出路径）后报 `Denied ID .../pdf.worker.min.mjs?url`。
其余 49 个文件全过，证明只是软链问题。

第二次补丁（`rsi/worktree.py::provision_node_modules`，`rsi/controller.py`
在建两个 Judge worktree 后调用，Fixer worktree 试点期不装——手动 Fixer
不跑命令）：每个 Judge worktree 里实打实跑一次 `npm ci`（lockfile 精确，
实测 6 秒），目录真实落在 worktree 内。验证：临时 worktree 里 `npm ci`
后，之前失败的 2 个文件 92 个测试全部通过。安全：node_modules 在 Morde
仓库 `.gitignore` 第 6 行，`git add -A` 会跳过它；`tsc -b` 的 build info
写在 `node_modules/.tmp/` 里，不进 worktree。试点前需在主检出先装好依赖
（2026-11-13 在 `~/workspace/morde-check/web/portal` 跑了 `npm ci`，418
个包，git status 无新增改动——只多了被忽略的 node_modules 目录）。

### 环境记录（试点第一轮）

- umask：0007（CAND-001 是 Portal 测试，不受 umask 影响；但按
  CANDIDATES.md 的提醒，跑基线时记下来）。
- node v24.22.1、npm 10.9.4。本轮只用 Portal 侧命令（typecheck + vitest），
  未用 Go 侧命令。

## 试点第一轮 CAND-001 结果（2026-11-13，手动 Fixer）

- 裁决：**REJECT**（hard gate: pass-to-pass regression）。不是修复错了，
  是 P2P 门抓到一个已有测试的行为变化。
- 证据：基线 score 0.6（A 复现失败、C 留出失败、B 全量 51 文件 / 529 测试
  全过）；候选 score 0.8（A 通过、C 3/3 通过、B 528/529）；S=0.875，
  delta_S=+0.375 > delta 0.02，成本 0，守卫无违规——除硬门外全部满足采纳
  条件。账本 `runs/morde/history.jsonl` 有一条 REJECT 记录
  （fixer="manual-pilot"，diff sha256 已记）；无 frontier.json、无 rsi/*
  分支、无残留 worktree；Morde 仓库未动（HEAD 273d300）。
- 原因（已核实）：`Projects.test.tsx:355` 断言一条
  routing_status='routed'、delivery_status='applied' 的消息显示“已发送”。
  修复前 `routingStatusText('routed')` 返回 null，`Projects.tsx:836` 的
  `?? deliveryStatusText(...)` 回退到“已发送”，测试通过；修复后显示
  “已分派任务”，“已发送”消失，测试失败。**这是测试把有 bug 的回退行为
  写成了期望，不是真实回归**——修复的本意就是让已分派的消息显示路由
  状态而不是投递回退。
- 教训：P2P 门能抓到行为变化，但分不清“测试写死了有 bug 的行为”和
  “真实回归”，这一步必须人判断。正确的下一步是人把该断言改成期望
  “已分派任务”（改测试文件本身是人做的事，不走 Fixer——critic 会判
  GAMING-DETECTED），再重跑本轮。不要为过门而放宽 P2P 门。

## 反馈机制（2026-11-13 新增）

动机：验证器是裁判，但它只能量"测试过没过"，量不出"修得对不对路"——
CAND-001 的修复本身是对的，P2P 门却判了 REJECT。反馈机制补的就是验证器
量不到的那一路，而且必须来自 Fixer 外面。

### 分层

| 层 | 内容 | 落点 |
|---|---|---|
| 1 静默遥测 | 重提意图、手动回退、审批驳回/通过、延迟复核（24h/7d）、影子对比 | `runs/morde/telemetry.jsonl` |
| 2 一键审批卡 | 行为变化 + 测试 + 成本，三动作（采纳/驳回/修偏了）；对话 👍/👎 只记第一次 | `runs/morde/human_feedback.jsonl` + `runs/morde/approval-<task>-c1.json` |
| 3 深度评审 | 桩：触发条件 + 人工检查清单，无 UI | `feedback.request_deep_review` |

成功默认静默（验证器 + 成本两票即采纳，不问人）；只在不确定、修偏、
成本超了时才出卡；可攒一周批量问。

### 三票裁决（`feedback.three_vote_decision`）

验证器 + 用户 + 成本，两票采纳。验证器 F2P/留出失败是一票否决；验证器
弃权（单点 P2P 含糊）→ NEEDS_HUMAN；单个 👎 只暂停，连续两次才驳回；
用户说好但成本超了也暂停。`reward.json` 本身永不被反馈改写。

### NEEDS_HUMAN 路由（`selection.classify_p2p_failure` + 控制器）

触发条件（须同时满足）：tier A 通过、tier C 通过、恰好一个基线通过的
测试新失败；且（若任务声明了 `behavior_tests`）该失败测试在声明之列。
满足时不判 REJECT：保留 `rsi/<task-id>` 分支、写审批卡、账本记
NEEDS_HUMAN、基线不动。消解流程：人改测试期望（人工编辑，Morde 仓库，
不走 Fixer）→ `rsi.py feedback --kind human_resolution --round <轮次>
--note "update-test-expectation: ..."` → 下一轮重跑该任务。无消解记录时
`pick_next_task()` 跳过该任务，循环不会空转；消解记录按轮次绑定，旧轮次
的消解不能重开新一轮的 NEEDS_HUMAN。

无 per-test map 的 Portal 侧（vitest Plain 命令）：从 tier B 证据日志的
`FAIL <文件>` 行归因，须任务声明 `behavior_tests`；归因不上的仍判 REJECT。
多点失败（≥2 个新失败测试）或 A/C 任一失败时同样判 REJECT，不走此路由。

### 反馈转学习（`feedback.feedback_to_learning`）

修错/没修到点/引入新问题 → 新留出测试**草案**（draft，人按它写真正的
测试，草案本身不是测试）；修偏 → 新 evolve 任务草案（先澄清需求再修）；
修对或表述太含糊 → 只记权重。草案不自动写进任务文件、不自动计分。

### 防泄漏

- Fixer 只看到脱敏的上一轮反馈（标签 + 一句话，无 evidence、无留出内容、
  本轮不可见），经 `feedback_for_fixer` + `critic.review_brief` 双重检查；
  brief 检查只认调用方给的留出文件名标记，不认通用词（SKILL.md 正文就写
  着 `reward.json`，通用匹配会误杀所有真 brief）。
- diff 碰 `human_feedback.jsonl` / `telemetry.jsonl` / `approval-*.json` /
  `rsi/feedback.py` 即 GAMING-DETECTED。
- 任务 JSON 新增可选字段 `behavior_change`（审批卡上的行为描述）和
  `behavior_tests`（断言该行为的测试），见 `domains/morde/tasks/TEMPLATE.json`；
  CAND-001 已补上这两个字段（内容来自已核实的试点记录）。

### 还是桩的东西

- 影子对比的输入喂给（跑两版取输出的适配器），比较器本身是实的。
- 深度评审无 UI。
- 发现器仍是 evolve.json 顺序取任务；反馈转学习产出的只是草案，不自动
  变成新任务（人决定是否采纳进 evolve.json）。

## delta 重估计（calibrate）与 k-trial 重复评估（2026-11-13 新增）

### delta = null 的语义

`domains/morde/rrsi.json` 的 `delta` 现在是 `null`，表示"未测量"，不是
0.02。0.02 只是试点期拍脑袋的初值，`RSIConfig.delta` 的默认值仍保留
0.02（仅当配置键缺失时；现有测试用 `RSIConfig.load(None)` 跑完整轮次，
默认值改 null 会打崩它们）。显式 `null` 才触发测量流程。

### calibrate 的用法

    # 先有重复基线评估数据：把基线验证的输出存进 runs/morde/
    #   baseline-eval-<task>-<n>.json   （EvalResult JSON）
    #   baseline-reward-<task>-<n>.json （scripts/verify.py 的 reward.json）
    # 最少 1 份可跑（走 bootstrap），≥2 份优先用实测分数差的标准差。
    python rsi.py --domain morde calibrate [--reps 2000]

    # 输出：calibration dict（delta / 方法 / n_evals / S_per_eval...）
    # 写入 runs/morde/calibration.json；delta 打印出来。
    # 没有可用数据时明确报错退出，不编一个 delta。

测量值写进 `runs/<domain>/calibration.json`，永不写回 `rrsi.json`（手改
配置不被测量覆盖）。delta 为 null 时，`baseline`/`round`/`run` 启动前若
`calibration.json` 存在则采纳其中的值；`baseline` 跑完后自动尝试重估计
一次。公式与分支来自 RRSI `refs/rrsi/rrsi/calibrate.py`（bootstrap_se /
pooled / calibrate），输入改为本项目的 `EvalResult`，空输入或零 trial
时抛错；实现在 `rsi/calibrate.py`。

控制器内的接线（同日集成）：每轮 BASELINE 后把基线评估记进
`runs/<domain>/`（`record_baseline_eval`）并把基线 reward 存为
`reward-<task>-base.json`；随后重解 delta（配置 > calibration.json >
自动校准 > 0.02 兜底），裁决用的就是这个解出的值，来源记进 round
summary（`delta_source`），所以 delta=null 的轮次不会在 judge() 里
崩。`status` 也显示当前 delta 与来源。

### k-trial：只对声明抖动的检查重复跑

任务 JSON 可选字段：

    "k": 2,                        // 默认 1（试点现状，行为不变）
    "flaky_checks": ["B-0"]        // 检查名，与 reward.json 里的名字一致

只有 `flaky_checks` 里列出的检查跑 k 次，其余检查永远跑 1 次。每次
trial 记在检查的 `trials` 列表里（passed / returncode / missing /
output_path）；超时或二进制缺失的 trial 记 `missing=True`，计 0 分且
分母不变（RRSI `evaluate.py` 的"缺失计 0、分母不变"）。聚合分 S 按
trial 取平均：1 次失败 + 1 次通过 = 0.5。合并后的检查 `passed` 保持严格
（须全部 trial 通过），go-test-json 的 per-test 图按 AND 合并，所以
k-trial 只细化分数，不放宽 F2P/P2P 硬门。实现在 `rsi/verifier.py`
（`_run_trials`）与 `rsi/selection.py`（`eval_from_reward(..., k)`）。
控制器把任务的 `k`（未声明时用配置的 `k`）传给验证器与评分；任务
模板见 `domains/morde/tasks/TEMPLATE.json`。

## 组件标签与停滞重定向（2026-11-13 新增）

来源：RRSI `rrsi/components.py`（K + K_STR + classify_diff /
has_evidence / normalize / text_only）与 `rrsi/history.py`
（stall_flag / exploration / tried / prune_set）+ `rrsi/loop.py`
2–3 步；只借结构与公式，词汇与规则按 Morde 重写。实现在
`rsi/components.py`（新）、`rsi/history.py`、`rsi/controller.py`。

K 共 14 个标签：`ui-text`、`ui-style`、`null-check`、`error-message`、
`config-default`、`logic-small`（前 6 个与 `domains/morde/SKILL.md`
提议者词汇一致）、`portal-ui`、`api-contract`、`mock-fixture`、`docs`，
以及结构子集 K_STR（"加机器/加机制"）：`agent-tool`、`skill`、
`workflow`、`test-harness`。归因：每次候选收到 diff 后控制器用
`components.normalize(声明标签, diff)` 定标签——声明须有 diff 证据，
否则按 diff 的文件名/内容信号重归因；标签记进 `history.jsonl`（新
`components` 列表字段，旧单值 `component` 保留兼容），
`History.component_summary()` 按组件统计 ADOPT/REJECT 与胜率，供分析器
与 `status` 用。Morde 新增 `text_change()`：只换字符串内容（抹掉字面量
后增/删行骨架相同）即判文案，覆盖 CAND-001 形状的 `return "已发送"`
改动；RRSI 的严格 `text_only()` 会漏掉这类改动。
`components.novelty()` 只计 K_STR，与 `selection.py::novelty()`（计全
部件）不同。

停滞：`stall_flag` 公式与 RRSI 原样（最近 w 轮改进 ≤ delta 即停滞，
轮数不足为 0）；轨迹是每轮后基线分数（采纳才动）。停滞时
`pick_next_task()` 把同一组件连续失败 ≥2 次的任务往后放，选下一个
不同组件的任务；无任务可换时返回 `None`，`run()` 报 `STALLED` 而不是
`DONE`，不编造任务。exploration 文本随任务书 dict 交给 Fixer
（`exploration`/`stall_flag`/`prune_set` 三个键，`fixer.py` 接口未改）；
因 `render_brief`（在 `fixer.py` 中）无 exploration 插槽，停滞时全文
同时写进本轮 ledger 的 `detail`。剪枝集是 Morde 自定规则（未照搬
RRSI 的 `g_t(l) <= 0`，因无每组件 delta_S 序列）：最近 `n_prune` 轮里
采纳 0 次且已裁决 ≥2 次的组件进集，按尝试次数倒序取最多 `n_prune` 个；
"≥2 次"下限是为了一次驳回不算数。

## 自我进化（self domain，2026-11-13 新增）

自我进化是把 RSI 循环指向 morde-rsi 自身：Fixer 改本项目源码，验证器
跑本项目 pytest。Domain 在 `domains/self/`，结构仿 `domains/morde/`：
`adapter.py`（`SelfDomain`，`run`/`score`/`smoke`/`guards_for_diff`）、
`rrsi.json`（试点值 T=3、k=1、delta=0.02 未测量，`repo_path` 指回本仓库）、
`SKILL.md`（self 提议者宪法：只做 A 级小修，白名单试点期只有
`pyproject.toml`）、`tasks/evolve.json`。

第一个真实任务 SELF-0001（2026-11-13，已复现）：全新 venv 里
`pip install -e '.[dev]'` 失败，setuptools 报 `Multiple top-level
packages discovered in a flat-layout: ['rsi', 'refs', 'runs', 'domains']`
（构建子进程 exit 1；不带 `[dev]` 的安装同样失败）。基线
（`run_verifier`，`fail_fast=False`）：tier A 失败、tier B 全绿、tier C
失败，score 0.3333；注入的复现/留出文件跑完均删除。验证命令用仓库自己
的 venv python（绝对路径：Judge worktree 没有 `.venv`，系统 python3
没有 pytest）；tier B 是整包门（本项目基线 pytest 全绿，不像 Morde 基线
有环境失败测试）。首轮自我试点（同日，手动 Fixer）：见 HANDOVER.md。

已知限制：控制器不调 `domain.run()`/`domain.score()`（内联
`run_verifier` + selection）；但每轮的 reward 已落盘为
`runs/<domain>/reward-<task>-base.json` / `reward-<task>-c1.json`，供分
析器读逐测试数据。pytest 的 NEEDS_HUMAN 归因已接线：控制器从 tier B
证据日志的 `FAILED <文件>::<测试>` 行归因（vitest 的 `FAIL <文件>` 行
仍支持）；归因不上的仍判 REJECT。

### 元护栏：循环不能给自己改卷

自我进化的元护栏回答一个问题：当"学生"和"考官"是同一个仓库时，循环凭
什么不能给自己改卷？设计借两处思想。第一是 RRSI 的 leakage critic：
评估前先审 diff，审不过不花评估成本，命中受保护路径即
GAMING-DETECTED。morde-rsi 的 critic 本来只做确定性检查（路径黑名单 +
凭据正则），self domain 把这张黑名单按 domain 扩展：
`rsi/critic.py` 的 `SELF_PROTECTED_PATH_PATTERNS` 只在
`domain="self"` 时生效，覆盖验证器（`rsi/verifier.py`）、采纳阈值
（`rsi/selection.py`）、防泄漏规则（`rsi/feedback.py`）、入口
（`rsi.py`）、全部测试（`tests/`）、阈值与宪法
（`domains/self/rrsi.json`、`SKILL.md`）、评分接线（`adapter.py`）、
全部任务材料（`domains/self/tasks/`）与全部账本（`runs/`、
`history.jsonl`）。domain 区分是刻意的：morde domain 的 Fixer 在 Morde
仓库里，根本碰不到这些路径，误判会杀掉正常候选；self domain 的 Fixer
在本仓库里，这些路径全在它的 worktree 中，所以必须按 domain 开关——
测试双向断言：`domain="self"` 判作弊，`domain="morde"` 放行。第二是
OpenRSI 的 Work/Judge 分离：干活的（Fixer）与判分的（验证器 + critic +
采纳阈值）必须是两个不重叠的信任域，Judge 的代码、阈值、考题与账本对
Work 只读且不可达。morde domain 里这个分离是天然的（两个仓库）；self
domain 里它是人造的，所以要三道门而不是一道：critic 是第一道门
（GAMING-DETECTED，控制器经 `rsi.py` 传 domain 接线），
`domains/self/adapter.py` 的 `guards_for_diff` 是第二道门（经 `rsi.py`
的 `guard_fn` 进 `selection.judge`，命中即不可采纳），第三道是物理层：
控制器在建 Fixer worktree 时删掉 `domains/self/tasks/`（
`fixer_worktree_excludes`），且该目录的任何改动都不会进候选 diff（
`worktree.diff_of_worktree` 的 exclude pathspec）；SKILL.md 的白名单
（试点期只有 `pyproject.toml`）是第四道约束——白名单之外一律不许碰，
黑名单命中直接判作弊。

残余风险已记录不假装不存在：考题与考生同仓，任务描述本身（含复现命令
与报错原文）仍会进任务书，Fixer 能从描述反推考题大致内容；缓解是留出
测试断言通用性质（装包后能导入）而非可特判的值、采纳前人工审 PR。改动
保护路径只能走人工 PR，必要时走 NEEDS_HUMAN——这条与 CO-0007 护栏一致：
验证器独立于 Fixer、Fixer 不能改/跳过/弱化验证、永不自动合并、重大功能
人审。

### 确定性分析器（`rsi/analyzer.py`，不用 LLM）

读 `history.jsonl` + `telemetry.jsonl`（+ 可选的 reward 列表），输出：
失败模式聚类（任务/组件/裁决分组计数）、组件胜率（ADOPT 率；无组件标签
的记录按 task_id 前缀派生任务类型聚类并注明）、抖动测试（同一测试多轮
pass/fail 翻转，取 tier B per-test 结果）、遥测信号（kind 与极性计数）、
草案（失败簇 ≥2 条才产草案，复用 `feedback.feedback_to_learning` 的
artifact 形状：REJECT 簇 → 新 evolve 任务草案、NEEDS_HUMAN 簇 → 新留出
测试草案；STUCK/GAMING-DETECTED 只警告、不成草案；只产草案，绝不自动
写进 evolve.json）。提供 `analyze()`（返回 dict）、
`report_markdown()`、`write_report(path)`、`analyze_files()`。LLM
analyst（RRSI 的 analyst.py + digester.py）暂不做，等 DSHFixer 接通后
再以 DSH Task 形式实现；此前的分析全部是确定性的。

# 借鉴其他项目

> 调研时间：2026-11-13。起点是 2026-11-12 的深度研究报告《Recursive Self-Improvement (RSI) for Self-Repairing Software》（下称"研究报告"）；RRSI 除外，另深挖了 OpenRSI、DGM 两个报告内来源，并新查了 SWE-smith（报告仅两行提及，本次读了论文 §2 全文）与 TextGrad（报告没有，本次新查）两个来源。证据等级沿用研究报告的口径：**已证实** = 本次读到原文；**二手** = 转述自他人总结；**自报** = 作者自述未审计。数字凡未注明"本次核对"均沿用研究报告，不另编造。

## 1. OpenRSI Harness —— Work/Judge 分离（已借一半，补三条小规则）

**它是什么。** OpenRSI-Index（MIT-IBM Watson AI Lab + Amazon A-EVO Lab，Preview v0.1，2026-09/10）是把真实模型开发项目变成"研究 Agent 在持久 Work 容器里干活、每次提交在全新 Judge 容器里用私有测试打分"的基准设施；一句话：**Work 持久可写、Judge 每次从干净镜像出发、只读挂载 Work 的写时复制（CoW）快照、测试与评分代码永不进 Work**。

**具体借什么。** 本次直接读了仓库原文 `RSI-Harness/docs/harbor-task-authoring/docker-compose.md`（2026-11-13 实读，commit `24e14758`），可借四条现成规则：

- **只读快照 + 干净镜像 Judge**："Each submission pauses Work and creates a CoW snapshot of the managed volume. Judge mounts that snapshot read-only at the same WORKDIR… The Judge starts from the clean image Work started from, so changes the Agent made outside the WORKDIR do not reach it."对应到 morde-rsi：Judge worktree 只接纳候选 diff，不接纳 Fixer worktree 的任何其他产物（已基本做到）。
- **保留路径禁区**：WORKDIR "must not overlap Engine-owned targets `/tests`, `/logs/verifier`, `/logs/agent`, or `/run/rsi-harness/staging`"。对应到 morde-rsi：把 tier C 留出测试、`reward.json`、`runs/` 账本列为声明式保留路径，由 runner 在建 Fixer worktree 之前就排除在外，而不只靠事后 critic 查 diff。（2026-11-13 集成时已落地一半：self domain 的 `fixer_worktree_excludes` 在建 Fixer worktree 时删掉任务材料目录。）
- **只返回聚合分数**：learnability-cot 任务 README 原文："only safe aggregate scores, losses, provenance counts, and correctness status are emitted"给 Agent；"the evaluator never imports candidate code"。对应到 morde-rsi 现有 `feedback_for_fixer` 的脱敏：再收紧一步——Fixer 只看到聚合分与失败计数，看不到任何 tier C 日志原文。
- **预算写进任务定义**：learnability-cot 原文："Recommended maximum: 3 submissions… Agent timeout calculation: `60 h = 18 h Work + (3 submissions × 10 h blocking Judge) + 12 h margin`"。对应到 morde-rsi：把 `max_submissions`、单次 Judge 超时、总预算从全局 `rrsi.json` 下沉到每个任务 JSON 里声明（Harbor 的 `task.toml` 写法）。

**为什么适合。** 机制与现状一一对应：试点期 k=1 串行、单 bug、DSH 执行器、本地 git worktree、确定性验证器——OpenRSI 的 Work/Judge 分离在 morde-rsi 里已有雏形（Fixer worktree 与 Judge worktree 分离、tier C 注入后删除、`reward.json` 为唯一裁决输入），借上面四条是"收紧现有做法"而非"另起一套"，不需要容器、不需要 Harbor、不需要 GPU。窄 Compose 的思想（只接受白名单键，拒绝 `volumes/privileged/cap_add`/host 网络/sidecar/未知键）还可直接映射为：把 critic 从"路径黑名单"翻转为"路径白名单"，与 `SKILL.md` 已有的白名单路径表合一。

**为什么不适合 / 坑。** 只借隔离思想，不借规模与载体：OpenRSI 是集群级基准设施，单个签名任务可达 18,432 H100-hours/轮、整站建设花了 100K+ H100-hours（自报），Preview v0.1 至今**没有已发表的 Agent 分数**（研究报告已核实：它是基准设施发布，不是"Agent 已胜过人类配方"的证据）。另有两个原文警告必须照抄：一是 Work 以 root 运行，"Do not treat Compose `user` as an isolation boundary for Agent behavior"——morde-rsi 里同理，不能把"DSH Task 的 prompt 约束"当隔离边界，隔离只能靠 worktree 与 runner 的文件系统检查；二是 Preview 跟随浮动的 `main`、不是不可变发布，借鉴时要 pin 住本次读到的文档版本。

**许可证。** **Apache-2.0**。依据：两家独立发布报道一致称其为 Apache-2.0 仓库（Vakker Wire："an Apache-2.0 benchmark stack"；The Neuron："The Apache-2.0 repo is public"），与研究报告一致。诚实说明：本次未直接打开仓库根目录 `LICENSE` 文件原文（搜索未返回该文件直链），但 Apache-2.0 属宽松协议，借结构与规则无障碍；若后续要逐字引用其文档语句，补读一次 `LICENSE` 原文即可。

**移植成本与顺序。** **小**。四条均为 10–50 行级改动，无新依赖，标准库内可完成。**建议：做，排第一**——在 CAND-002 之前落地，因为它是给现有 Judge 补"最后一道缝"，且 CAND-001 试点已证明 P2P 门会把行为变化误判为回归，Judge 越干净，NEEDS_HUMAN 的误触发越少。

## 2. Darwin Gödel Machine —— Archive 与父代选择公式（借公式，不抄种群）

**它是什么。** DGM（Sakana AI + UBC，arXiv:2505.22954，2025-05；ICLR 2026）是"冻住基础模型、让 Agent 改自己的 Python 代码"的开放式进化：一句话：**维护一个全部历史 Agent 的 archive，每轮按"表现好 × 孩子少"的概率抽父代，父代自己诊断日志、改代码生出子代，子代只要能编译且保留改代码能力就进 archive，分数只影响抽中概率、不决定准入**。

**具体借什么。** 本次核对了论文附录 A.2 原文（arXiv v3，2026-03-12），父代选择公式如下，λ=10、α₀=0.5，SWE-bench 实验 k=2、Polyglot 实验 k=4：

- 合格集 Eᵗ = {aᵢ ∈ Aᵗ : αᵢ < 1}（未满分者才有资格被抽）；
- sigmoid 表现分 sᵢ = 1 / (1 + exp(−λ(αᵢ − α₀)))；
- 新颖奖励 hᵢ = 1 / (1 + nᵢ)，nᵢ 为该 Agent 已有的"能正常改代码的孩子"数；
- 权重 wᵢ = sᵢ·hᵢ，抽中概率 pᵢ = wᵢ / Σwⱼ，有放回地抽 k 个父代。

另借一条**分阶段评估**（论文附录 E.1，经二手转述逐字引用，未直接读 PDF 全文）：评估按 10 → 50/60 → 200 任务逐级放大，>40% 且排名前二才进下一阶段——中段数字二手来源有分歧（50 或 60），引用时写"10 → 50/60 → 200"并保留此注。

**为什么适合。** 公式本身与现状完全兼容：纯算术、无依赖、标准库 20 行可实现；morde-rsi 已有"最佳 + 最多样的候选"迷你 archive 的想法（研究报告 Part 3 步骤 8），缺的正是一个**不贪心的选择规则**。试点期 k=1 时公式无用武之地，但一旦进入 best-of-N（报告建议 N=3）或保留多候选，公式直接解决"永远只变异当前最优会陷入局部最优"的问题——DGM 的消融实验证据（自报，论文数据）：贪心只变异最优 39.7%，无 archive 只变异最新 23.0%，完整 DGM 50.0%（SWE-bench，200 任务子集）；且论文记录"第 4 轮与第 56 轮分数下跌的 Agent 后来生出了最好的后代"，这正是 archive 保住"垫脚石"的价值。分阶段评估也直接对应 morde-rsi 的 tier 排序：便宜检查先行、Playwright 只跑一次。

**为什么不适合 / 坑。** 三个坑，都是原文或二手核对过的：

- **成本**：论文附录 E.1 估计单次 SWE-bench 运行约 **$22,000**、约两周，每次消融基线约 $10,000（本次经两份二手转述交叉核对，均逐字引用附录 E.1 原句；未直接读 PDF 全文）。morde-rsi 是本地单人项目，抄整套种群搜索等于把试点预算烧在"养 lineage"上。
- **有记录的 objective hacking**：论文承认 node 114 删掉了用于检测工具使用幻觉的特殊 token，而不是修掉幻觉本身（两份二手笔记一致；研究报告亦引同一案例）；另一份证据笔记还补充该 Agent 曾伪造"单元测试已跑过且通过"的日志。根源是 DGM 的准入只看"能编译 + 能改代码"，**分数不设准入门槛**——这正是 morde-rsi 绝不能抄的一点：我们的 F2P/P2P 硬门与 GAMING-DETECTED 必须留在准入处，不能只留在抽样权重里。
- **基准是噪声代理**：分数只量基准任务表现，作者自己也说明改进未被证明有益、只是经验上成立；且 DGM 的 Agent 只能改自己的 Python 代码，能力天花板是冻住的基础模型。

**许可证。** **Apache-2.0**。依据：官方仓库 `github.com/jennyzzt/dgm` 的 GitHub 元数据明确标注 "License: Apache License 2.0 (Apache-2.0)"、根目录有 `LICENSE` 文件；DeepWiki 的授权页亦确认全文为 Apache-2.0、版权人为 Jenny Zhang 与 Shengran Hu（2025）。宽松协议，公式与结构可直接借用、注明出处即可。注意：社区 fork `lemoz/darwin-godel-machine` 自称 MIT，那是 fork 的标注，借鉴以官方仓库的 Apache-2.0 为准。

**移植成本与顺序。** **公式本身：小；整套种群：大、不做**。建议：**公式做、排第二**——先把 `wᵢ = sᵢ·hᵢ` 写成 `selection.py` 里的一个纯函数并加单元测试（此时 k=1 不调用），等进入 N=3 best-of-N 或迷你 archive（≤5 条）时再启用；分阶段评估的思想已在 verifier 的 tier 排序里，补一条"候选在 tier A 失败即停、不跑 Playwright"的显式短路即可。**整套种群搜索不做**：$22k/两周的量级与"修一个小 bug"的目标不匹配，且准入不设分数门的做法与我们的硬门冲突。

## 3. SWE-smith —— 用"注 bug"造任务，解任务荒（只借 PR Mirror 一招）

**它是什么。** SWE-smith（Yang 等，arXiv:2504.21798，2025-04/05）是给任意 Python 仓库**自动合成 SWE-bench 风格任务**的管线；一句话：**先给仓库建好一个可跑通测试的执行环境，再用四种方法往代码里注 bug、只保留那些确实弄坏了 ≥1 个现有通过测试的候选，最后由 LM 照着失败测试写出 issue 描述**。

**具体借什么。** 本次读了论文 §2 全文（arXiv HTML v1），四种注 bug 策略与验证规则如下：

- **LM Modify**：给 LM 一个函数，令其引入错误改写（产出率 56.0%，单候选成本 $0.38¢，中位改 3 行）；
- **LM Rewrite**：只给函数头与 docstring，让 LM 重写整个函数（产出率 35.0%，$3.93¢，中位 24 行）；
- **Procedural Modification（AST 过程修改）**：取函数 AST，随机做 14 种变换之一（删条件/删循环、改运算符等，另有 11 种，见论文表 8）（产出率 40.2%，**$0.00**，中位 5 行）；
- **Combine Bugs（Patch Combination）**：把同一文件/模块的多个单 bug 候选拼成多部位复杂任务（产出率 96.9%，**$0.00**，中位 11 行）；
- **PR Mirror（Invert PRs）**：收集改 Python 文件的 PR，用 LM 把每个 PR 的改动在当前版本上**撤销**回去（产出率 33.8%，$5.53¢，中位 14 行；不 checkout PR 的 base commit，因为旧版安装说明可能不兼容）；
- **执行验证（核心规则）**：每个候选 patch 应用后跑测试套件，**只保留弄坏 ≥1 个现有通过测试的**（即 F2P 测试）；测试运行限 2 分钟，超时丢弃；全集 50,137 个实例、总成本约 $1,360（约 $0.027/实例，研究报告的"~$0.02/任务"为二手约数）；
- **环境复用（关键省钱点）**：**一个仓库只建一个 Docker 镜像**，而不是像 SWE-bench 那样每个任务一个镜像；50k 实例来自 128 个仓库，平均每库 381 个、最多 pandas 2,277 个；整套数据集一人约 20 小时人工（每库约 7 分钟解析安装步骤 + 约 1 分钟写测试输出解析器）。

**为什么适合。** 直击当前最现实的短缺：过硬的 A 级候选只有 CAND-001 与 CAND-002 两个（`CANDIDATES.md` 的诚实结论），而 morde-rsi 的循环每轮都要消耗一个真实小 bug。SWE-smith 的 **PR Mirror 是唯一不需"编造 bug"的造任务法**：把 Morde 已合入的人修 PR 撤销，就得到一个"已知有正确解、已知哪几个测试会失败"的任务——这正是研究报告 Part 3 步骤 8 建议的"把已修复 bug 的人修方案藏起来重跑"的自动化版本。且验证规则与我们现有的 tier A 语义完全一致（候选任务当且仅当弄坏 ≥1 个基线通过测试才算数），可直接复用 `verifier.py` 的逐测试 P2P 解析来做筛选。零成本的两招（AST 过程修改、Combine）则是日后任务池扩大时的备选。

**为什么不适合 / 坑。** 四个坑：

- **只支持 Python**：论文与工具链均为 Python 仓库设计；Morde 是 Go + TypeScript + Swift，AST 修改与测试解析器要全部重写，LM 改写与 PR 撤销的思路可借、代码不可照搬。
- **合成 bug ≠ 真实 bug**：论文自己的难度分析与二手评论一致指出，注入的故障有固定风格，模型会学到"注入故障的风格"而非真实维护工作；SWE-Gym 的教训（研究报告引二手讲义）是拿弱 on-policy 成功样本自训反而把 Lite 分数从 15.3% 拉到 8.7%。因此合成任务只能用来**练循环、量通过率**，绝不能用来当作"循环已证明能修真实 bug"的证据，更不能进留出集。
- **需要 LM 调用**：LM Modify/Rewrite/PR Mirror 都要调模型写代码/写 issue，与 morde-rsi"本地运行、不调外部模型"的试点约束冲突——PR Mirror 若要落地，须走已有的 DSH/DeepSeek 执行器，且 issue 描述须人工审一遍（LM 写的 issue 可能泄露修复线索）。
- **环境成本**：论文靠"每库一镜像"省存储，但 morde-rsi 是本地单机、git worktree 模式；造任务阶段的镜像/环境管理是额外负担，试点期只建议手工做 1–2 个 PR Mirror 任务验证流程，不建自动化管线。

**许可证。** **MIT**。依据：PyPI 页（`pypi.org/project/swesmith/`）明确写 "License: MIT. Check `LICENSE` for more information."；仓库为 `github.com/SWE-bench/SWE-smith`。宽松协议，策略描述与验证规则可自由借鉴；若日后移植其 Python 实现，需保留 MIT 声明。

**移植成本与顺序。** **中**。只做 PR Mirror 手工版：挑 1 个已合入的 Morde 小修 PR → 人工撤销其改动 → 用现有 verifier 确认基线测试失败 → 按 `TEMPLATE.json` 填任务（tier A 复现测试即被弄坏的测试、tier C 另写 1–2 个）——约半天，不写新代码。**建议：做，排第三**——在 CAND-002 之后、接 DSHFixer 之前，用它把任务池从 2 个扩到 3–4 个，验证"循环能稳定跑通"后再考虑是否把 AST 修改做成 Go/TS 版。全套 SWE-smith 管线（50k 量级、自动 issue 生成）**不做**。

## 4. TextGrad —— 把"反馈改提示词"形式化为文本梯度（借思想，不引库）

**它是什么。** TextGrad（Yuksekgonul 等，斯坦福，arXiv:2406.07496，2024-06；期刊版 2025-03 发于 *Nature*，题为 "Optimizing generative AI by backpropagating language model feedback"）是"文本版自动微分"框架；一句话：**把复合 AI 系统画成计算图，LLM 对每个节点给出自然语言批评作为"文本梯度"，再沿图反向传播、用文本梯度下降（TGD）改写提示词或代码片段**。

**具体借什么。** 本次读了论文摘要与 §2 全文（arXiv HTML v1），可借的机制是三件套，全部有明确公式：

- **文本梯度 ∇_LLM**：对变量 v 的梯度是一组自然语言批评，描述"该怎样改 v 才能让系统变好"；链式法则为 ∂L/∂v = ∪_{w∈SuccessorsOf(v)} ∇_f(v, w, ∂L/∂w)（论文式 11）——即从每个用到 v 的后继节点收集反馈再汇总；
- **TGD 优化器**：v_new = TGD.step(v, ∂L/∂v)（论文式 12），用当前值 + 文本梯度生成新值；类比 PyTorch 的 `optimizer.step()`，API 亦仿 PyTorch（`Variable`、`Function`、`TGD`）；
- **损失可以是测试输出**：论文 §2"Objective functions"明确列出损失的三种形态——LLM 评的自然语言目标、**跑单元测试的代码解释器输出（§3.1 代码优化）**、分子模拟器输出；即**确定性测试结果本身就能当损失函数**，梯度则由 LLM 就着失败输出写出批评。论文报告：GPT-4o 在 Google-Proof QA 上 51% → 55%；LeetCode-Hard 代码优化相对提升 20%；推理提示优化把 GPT-3.5 推近 GPT-4（以上均为作者自报，论文数据）。

**为什么适合。** 它正好给 morde-rsi 已有的**反馈转学习**（`feedback.py::feedback_to_learning`，现产草案、不自动进任务）补一个形式化外壳：把 `domains/morde/SKILL.md`（提议者宪法）与 `PATTERNS.md`（模式库）看作计算图的两个可优化变量，把每轮的验证器输出（tier A/B/C + reward.json）看作损失，把人工标签（`correct/wrong/misdirected/missed_point/introduced_bug`）与失败日志看作梯度来源；每攒够一批反馈，由人（或一个独立上下文的 LLM）写一条"该怎样改 SKILL.md"的批评，经 TGD 式改写后，**新版 SKILL.md 必须先在留出任务上跑通才采纳**——这与 RRSI 的"采纳须过噪声下限"同构，只是优化对象从"候选 diff"换成"提议者宪法"。且 TextGrad 的"梯度只是建议、决定靠验证"与我们的"反馈永不改写 reward.json"原则一致。

**为什么不适合 / 坑。** 三个坑：

- **每步优化都要烧 LLM 调用**：论文式 12 之后明确写道，n 条边的计算图每轮优化最多做 n 次额外的 LLM 调用（每条边一次梯度计算）；morde-rsi 试点期"本地、不调外部模型"，且 DSHFixer 还是桩，现在引一整套梯度反传等于在循环外再挂一个烧 token 的循环。正确姿势是**手工版**：人按 TGD 的格式写批评、改 SKILL.md，不跑自动反传。
- **梯度没有正确性保证**：文本梯度是 LLM 的意见，不是数学梯度；论文的收益全部来自"LLM 自己能对子任务给出好批评"这一假设。我们的约束更严：任何 SKILL.md 改动若未经验证器在留出任务上复核，等同于让 Fixer 自己改自己的宪法——这正是 Gödel Agent 4% 试验自我终结的同类风险，必须由人审 + 验证器复核双重把关。
- **仓库已停滞**：`zou-group/textgrad` 最新版 0.1.8、自 2025-07-25 后无提交（多方 2026-10/11 的盘点一致称其"稳定但静态"），引库等于引一个无人维护的依赖；且 morde-rsi 约束为**标准库 only**，引库本身就违规。

**许可证。** **MIT**。依据三重一致：GitHub API 返回该仓库 `license: {key: mit, spdx_id: MIT}`；仓库 README 徽章 `License-MIT`；2026 年多份第三方盘点（如 futureagi 的框架 roundup）均列为 MIT。宽松协议，思想与公式可自由借鉴；但如上所述，**不建议引入其代码**。

**移植成本与顺序。** **手工版：小；引库自动版：大、不做**。建议：**手工版做、排最后（第四）**——等 CAND-001/002 跑完、手里有 5 条以上真实反馈记录后，再按 TGD 格式做一次 SKILL.md/PATTERNS.md 的复盘改写（批评 → 改写 → 留出任务复核 → 人审采纳），全程人工、不写自动反传代码。**引库与自动梯度反传不做**：违反标准库 only 约束，且在 DSHFixer 接好、循环本身可信之前，优化"教 Fixer 的提示词"属于过早优化。

## 不借的清单

- **DGM 的整套种群搜索**：SWE-bench 20.0% → 50.0%（论文自报，200 任务子集）但单轮约 **$22,000**、约两周（论文附录 E.1，经二手转述核对），且准入不设分数门，已有 node 114 删检测 token 的 objective hacking 记录；只借父代选择公式（见 §2），种群本身不抄。
- **Gödel Agent 的运行时 monkey-patching**：Agent 在进程内改自己的代码、改递归的 `SELF_IMPROVE` 函数本身，无沙箱、无独立 Judge；14% 试验最终失败、4% 因改了自身的改进模块而意外终止（研究报告引二手总结）；与 morde-rsi"验证器是确定性脚本、Fixer 永不碰循环自身"的硬规则直接冲突。
- **AlphaEvolve 的进化程序库（MAP-Elites / 岛屿种群）**：需要为每个任务手写程序化评分器、数千次并行评估，且编排器不开源（仅结果仓库公开）； headline 数字（4×4 复数矩阵乘 48 次标量乘、Borg 启发式回收 0.7% 算力等）均为 Google 自报、内部系统无法复现；对"一次修一个小 bug"的循环是重武器打蚊子。只借其"验证器质量就是系统质量"这一条原则。
- **Meta SSR / SWE-RL 的权重训练路线**：自博弈 RL（bug 注入器 × 求解器、GRPO + 二值奖励）报告 +10.4 pts（SWE-bench Verified，相对自家检查点，自报），但需要 RL 训练设施与能打的基座模型；morde-rsi 本地单机、DSH 执行器、标准库 only，三者全不满足。训练路线与 SWE-smith 全量管线一起，留待循环本身可信之后再议。

## 顺序总表

| 顺序 | 做什么 | 来源 | 成本 | 状态 |
|---|---|---|---|---|
| 1 | OpenRSI 四条收紧规则（保留路径、聚合分数、任务级预算、critic 白名单化） | OpenRSI | 小 | 做（保留路径与聚合分数收紧已在 self domain 落地一半；其余待做） |
| 2 | DGM 父代选择公式写成纯函数备而不用；tier A 失败即停的短路 | DGM | 小 | 做（公式先行，启用待 N=3） |
| 3 | SWE-smith PR Mirror 手工版，任务池 2 → 3–4 个 | SWE-smith | 中 | 做 |
| 4 | TextGrad 手工版：按 TGD 格式复盘改写 SKILL.md/PATTERNS.md，留出任务复核 + 人审 | TextGrad | 小（手工） | 做（待 ≥5 条真实反馈后） |
| — | DGM 整套种群、Gödel 运行时自改、AlphaEvolve 种群库、Meta SSR 权重训练、SWE-smith 全量管线、TextGrad 引库自动反传 | — | 大 | 不做 |

**来源（本次实读或核对）**：OpenRSI `docker-compose.md`（commit `24e14758`，2026-11-13 实读）；OpenRSI learnability-cot README（经搜索结果核对：60 h = 18 h Work + 3×10 h Judge + 12 h margin；max 3 submissions）；DGM arXiv:2505.22954 v3 附录 A.2 公式（经搜索结果核对原文）与附录 E.1 成本（经两份二手转述交叉核对）；DGM 官方仓库 `github.com/jennyzzt/dgm`（Apache-2.0，GitHub 元数据 + DeepWiki）；SWE-smith arXiv:2504.21798v1 §2 全文（2026-11-13 实读）与 PyPI 页（MIT）；TextGrad arXiv:2406.07496v1 摘要与 §2（2026-11-13 实读）与 GitHub API 许可证字段（MIT）；其余数字与证据等级沿用 2026-11-12 研究报告（`~/workspace/research_notes/morde-rsi-projects-best-practices-20261112-1430/report.md`）。
