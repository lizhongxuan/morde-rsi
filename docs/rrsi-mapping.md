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
