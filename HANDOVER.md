# morde-rsi 交接文档

> 写给接手这个项目的下一个 AI。**只写已查到的事实**：凡是写了数字、结论或命令的，都是本文作者在 2026-11-13 对照源码、账本和实跑结果核对过的；没核对上的写“未验证/待确认”，提议不会写成已实现。
> 本文档生成时间：2026-11-13。项目位置：`~/workspace/morde-rsi/`。

---

## 1. 项目一句话

**morde-rsi 是让 Morde 自己修自己的 bug、并用独立验证器证明修对了的递归自我改进（RSI）循环**：发现 bug → Fixer 在隔离 worktree 里修 → 确定性验证器在全新 worktree 里打分 → 只有比基线好过噪声才采纳为新基线；修坏的进不来，用户随时能停，重大功能人审。

**为什么是独立项目，不是 Morde 模块**（这是用户 2026-11-12 定下的结构，见 `~/MEMORY.md` CO-0007 条目）：

- 放进 Morde 会违反 Morde `AGENTS.md` 写死的“DSH 是唯一 Agent Runtime”；RSI 出问题不能把 Morde 一起拖垮。
- 独立项目以后还能复用到别的项目上。

**和 Morde 之间只有 git + 文件三条线，不走 API、不联网**：

1. **读**：RSI 只读 Morde 仓库（源码、测试、构建命令、`AGENTS.md`），读不到运行中的用户数据。
2. **修**：Fixer 在 Morde 仓库的 git worktree 里改代码，候选只提交到 `rsi/<task-id>` 分支，永不自动合并；合入主分支是人工审 PR 的事。
3. **验**：验证器在全新的干净 Judge worktree 里跑测试，输出 `reward.json`。

Morde 本地检出在 `~/workspace/morde-check`（见第 6、7 节的硬规则与状态）。

---

## 2. 架构

文字版架构图：

```
发现器（桩：人填 evolve.json，顺序取任务）
    ↓
裁判长 Controller（rsi/controller.py，状态机）
    ↓
Fixer（rsi/fixer.py：DSHFixer 是桩；ManualFixer 是试点用人工模式）
    在自己的 git worktree 里修，只看任务书 + 白名单路径
    ↓
Critic（rsi/critic.py，评估前防泄漏；命中受保护路径 → GAMING-DETECTED）
    ↓
Verifier / Judge（rsi/verifier.py + scripts/verify.py，确定性脚本，不是 Agent）
    在全新干净 Judge worktree 里跑 Tier A / B / C，输出 reward.json
    ↓
Selection（rsi/selection.py：噪声下限 + 成本规则 + F2P/P2P 硬门）
    ↓
ADOPT（提交到 rsi/<task-id> 分支，写 frontier.json）/ REJECT / NEEDS_HUMAN / STUCK / GAMING-DETECTED
    ↓
证据账本（runs/morde/history.jsonl，每候选一条 JSONL；反馈见第 3 节）
```

### 各模块职责与关键文件

| 模块 | 文件 | 职责（已实现） |
|---|---|---|
| 裁判长 | `rsi/controller.py` | 外循环状态机：DISCOVER → BASELINE → FIX → VERIFY → ADOPT/REJECT → NEXT；预算、STOP 文件、runs/ 可续跑（`frontier.json` + `history.jsonl` 就是全部状态）；`pick_next_task()` 按 evolve.json 顺序取下一个未尝试任务；`delayed_recheck()` 真实现（采纳后延迟复核）；NEEDS_HUMAN 路由与审批卡落盘 |
| Fixer 接口 | `rsi/fixer.py` | 唯一的 Agent。`DSHFixer` 是桩（见第 5 节③）；`ManualFixer` 是试点用人工模式，账本标注 `fixer="manual-pilot"`；`render_brief()` 产出 DSH Task 任务书，DSHFixer 发任务书前先过 `review_brief`，泄漏报 `RuntimeError` |
| 验证器 | `rsi/verifier.py`、`scripts/verify.py` | Judge：确定性脚本。Tier A 构建 + vet + 指定单测（复现测试由验证器从工作区之外注入，跑完删除）；Tier B 回归（逐测试 P2P）；Tier C 留出测试（从工作区之外注入，跑完删除）。输出 `reward.json` |
| 防泄漏 | `rsi/critic.py` | 评估前确定性检查：路径黑名单 + 凭据正则 + 留出任务 id；LLM 评审已删掉（本地优先，不调外部模型），由“窄白名单 + 人工审 PR”替代；diff 碰反馈账本/审批卡/`rsi/feedback.py` 即 GAMING-DETECTED；`review_brief` 检查任务书是否带留出标记（只认调用方给的文件名标记，不认通用词，因为 SKILL.md 正文就写着 `reward.json`） |
| 选择 | `rsi/selection.py` | RRSI 算法 2：噪声下限 + 成本规则 + F2P/P2P 硬门；`classify_p2p_failure()`：单点 P2P 失败走 NEEDS_HUMAN（2026-11-13 新增，不是 RRSI 的东西） |
| 反馈 | `rsi/feedback.py`（669 行） | 见第 3 节：1 层静默遥测、2 层一键审批卡、3 层深度评审（桩）、三票裁决、反馈转学习、Fixer 可见反馈的脱敏 |
| 账本 | `rsi/history.py` | 一候选一条 JSONL；`VERDICTS = ADOPT / REJECT / STUCK / GAMING-DETECTED / SKIPPED / NEEDS_HUMAN` |
| 退火预算 | `rsi/schedule.py` | RRSI 公式原样：`b_t = ceil(b_min + (b_max-b_min)/2 * (1+cos(pi t/T)))` |
| worktree 管理 | `rsi/worktree.py` | git worktree 管理；`provision_node_modules`：每个 Judge worktree 里实打实跑一次 `npm ci`（约 6 秒，见第 3 节缺口 2） |
| 配置 | `rsi/config.py`、`domains/morde/rrsi.json` | 超参数：T=5、k=1、m=1、b_min=1、b_max=2、delta=0.02、beta0=0.1、beta1=44.5、max_submissions=3、time_budget_s=3600、timeout_s=600、fixer=dsh、fixer_model=deepseek-v4-flash、repo_path=`/home/hatch/workspace/morde-check` |
| CLI | `rsi.py`（仓库根） | 子命令见第 4 节 |
| Morde domain | `domains/morde/adapter.py`、`domains/morde/SKILL.md`、`domains/morde/PATTERNS.md`、`domains/morde/README.md` | Morde 适配器：任务列表、超参数、提议者宪法、守卫（`guards_for_diff()`：diff 碰审批卡/协调器/数据库迁移/凭据/DSH pin 直接不可采纳） |

### 与 RRSI 的关系

骨架来自 RRSI（`google-research/rrsi`，Apache 2.0，论文 arXiv:2609.24972，参考副本在 `refs/rrsi/`，已加入 `.gitignore`，不随仓库发布）。**没有 fork、没有整体复制代码**：核心模块是按 RRSI 算法重写的轻量版，对照见 `docs/rrsi-mapping.md`。借了：worktree 隔离、JSONL 账本、leakage critic、噪声下限、成本规则、退火预算、domain 适配器。改了：评估对象从“harness 在基准任务上的表现”改为“候选 diff 修 Morde bug 的效果”；删掉 LLM 评审；采纳只提交到 `rsi/<task-id>` 分支，永不自动合并；Fixer 是 DSH Task。没借的：`llm.py`、`analyst.py`、`digester.py`、harbor/Docker/Terminal-Bench/SWE-bench、`calibrate.py`（delta 固定 0.02 试点初值，未重估计）。

---

## 3. 当前真实状态（只写已验证的）

### 3.1 骨架完成，pytest 87/87 通过

- 语言：Python 3.10+，只用标准库（除测试依赖 pytest）。本文作者实测环境 Python 3.12.3。
- **pytest 87/87 通过，25.8 秒**（本文作者 2026-11-13 在 `~/workspace/morde-rsi/` 用自带 `.venv` 实跑：`87 passed in 25.76s`；另用全新 venv + 只装 pytest 复核：`87 passed in 25.46s`）。测试文件 9 个：`tests/test_controller.py`、`test_critic.py`、`test_feedback.py`、`test_fixer.py`、`test_history.py`、`test_schedule.py`、`test_selection.py`、`test_verifier.py`、`test_worktree.py`（另有 `conftest.py`）。`def test_` 共 78 个，收集到 87 项（含参数化）。
- 演进：47/47（骨架初验）→ 50/50（CAND-001 试点补了两个缺口）→ 87/87（反馈机制新增 37 个测试）。
- `rsi.py --domain morde smoke` 实测通过（2026-11-13 02:00 左右）：`ok: true`，go、node、npx、验证器脚本、仓库、portal package.json 都在，`evolve_tasks: 1`。注意：2026-11-13 早的 `runs/pilot-readiness-2026-11-13.md` 还记录这台 VM 缺 Go（`go: MISSING`），现在 Go 已在 `/usr/bin/go`，以本次实测为准。

### 3.2 CAND-001 第一轮：REJECT（修复本身是对的）

任务：`web/portal/src/app/message-status.ts` 的 `routingStatusText` 只处理了 `routing_status` 枚举 6 个值里的 5 个，`routed` 走 default 返回 `null`，导致已分派到任务的消息在聊天页不显示路由状态。修复：在函数里补 `routed` 分支，返回“已分派任务”，一行。任务定义见 `domains/morde/tasks/evolve.json`；候选池与证据见 `domains/morde/tasks/CANDIDATES.md`（候选 1）。

完整经过（已核实，账本 `runs/morde/history.jsonl` 只有这一条记录）：

| | 基线 | 候选 |
|---|---|---|
| score | 0.6 | 0.8 |
| Tier A（复现测试） | 失败 | 通过 |
| Tier B（回归，51 文件 / 529 测试） | 529/529 全过 | 528/529 |
| Tier C（留出测试） | 失败 | 3/3 通过 |

- 裁决：**REJECT**，`detail: "hard gate failed: pass-to-pass regression"`，`fixer="manual-pilot"`，`cost_seconds: 150.2`，`diff_sha256: 0dc1ec28973773e76a353a3195ec7a21b9506aeeefeee86529e25c2f61d22fa0`。除硬门外全部满足采纳条件（S=0.875，delta_S=+0.375 > delta 0.02，成本 0，守卫无违规）。
- 唯一失败的测试：`Projects.test.tsx:355`，断言一条 routing_status='routed'、delivery_status='applied' 的消息显示“已发送”。修复前 `routingStatusText('routed')` 返回 null，`Projects.tsx:836` 的 `?? deliveryStatusText(...)` 回退到“已发送”，测试通过；修复后显示“已分派任务”，“已发送”消失，测试失败。**结论：测试把有 bug 的回退行为写成了期望，不是真实回归。修复本身是对的。**
- 教训：P2P 门能抓到行为变化，但分不清“测试写死了有 bug 的行为”和“真实回归”，这一步必须人判断。不要为过门而放宽 P2P 门。
- 试点中发现并修复的两个骨架缺口（测试已覆盖）：
  1. **Tier A 复现测试注入**：复现测试是新建文件，仓库里没有，Fixer 又不许碰测试文件。补丁：验证器从工作区之外把复现测试拷进 worktree，跑完立即删除（`rsi/verifier.py` 的 `_inject_files()`，与 Tier C 共用）。
  2. **Judge worktree 缺 node_modules**：第一次补丁用软链是错的——Vite 的 module runner 拒绝真实路径在项目根之外的文件（pdfjs worker 报 `Denied ID .../pdf.worker.min.mjs?url`，2 个文件失败）。第二次补丁：每个 Judge worktree 里实打实跑一次 `npm ci`（`rsi/worktree.py::provision_node_modules`，约 6 秒）。
- 试点后状态：无 `frontier.json`（`rsi.py status` 显示 `frontier: {}`）、无 `rsi/*` 分支、无残留 worktree；Morde 仓库未动（试点时 HEAD `273d300`）。
- 环境记录：node v24.22.1、npm 10.9.4；umask 0007（CAND-001 是 Portal 测试，不受 umask 影响；但基线有 3 个 umask 敏感测试，见 CANDIDATES.md 备选 ALT-B）。

### 3.3 基线发现：Morde 基线本身就有环境依赖的失败测试

2026-11-13 在 `/tmp/morde-verify-clone`（`~/workspace/morde-check` 的干净克隆，HEAD 273d300）上量得：worker 包 46 个失败（43 个 `account Agent Runtime handshake is invalid`——缺 DSH 运行时握手；3 个浏览器超时 20–45 秒），app 包 1 个失败（`TestQuotaSettlePersistsOwnerStoragePauseBeforeAborting`，workspace 路径假设）。结论：**Tier B 绝不能是“整包必须通过”**，改为逐测试 P2P（基线通过的测试修完还得通过；基线已失败的不计入）。实现在 `rsi/verifier.py`（`go-test-json` 解析 + tier `mode: suite-ran`）和 `rsi/selection.py`（`test_map()` + `eval_from_reward()`）。换新 HEAD 后要重测这些数字。Portal 侧：基线 51 文件 / 529 测试全过；`playwright test` 仍未验证。

### 3.4 反馈机制（2026-11-13 新增，`rsi/feedback.py`，669 行）

动机：验证器只能量“测试过没过”，量不出“修得对不对路”——CAND-001 的修复本身是对的，P2P 门却判了 REJECT。反馈补的就是验证器量不到的那一路，而且必须来自 Fixer 外面。

| 层 | 内容 | 落点 |
|---|---|---|
| 0 验证器 | Tier A/B/C，`reward.json`（已有） | 证据日志 `runs/morde/evidence-*` |
| 1 静默遥测（无感知） | 重提意图 `detect_repeat_intent`、手动改回 `detect_manual_revert`（强负反馈）、审批驳回/通过 `approval_record`、延迟复核（采纳后 24h/7d 在新 worktree 重跑验证器，`Controller.delayed_recheck`）、影子对比（`shadow_compare` 是比较器，喂输入的适配器是桩） | `runs/morde/telemetry.jsonl` |
| 2 一键审批卡 | 复用 Morde 审批卡格式，不新发明 UI。卡上只有行为变化（不是 diff——`build_approval_card` 会拒收 diff 文本）、测试通过数、三层结果、成本、分数前后；动作最多三个：采纳 / 驳回 / 修偏了。对话里的 👍/👎 只记第一次（`record_chat_reaction`）。成功默认静默进预览不问；不确定、修偏、成本超了才问；可攒一周批量问（`batch_approval_cards`） | `runs/morde/human_feedback.jsonl` + `runs/morde/approval-<task>-c1.json` |
| 3 深度评审 | 桩：只有触发条件（重大功能、改动面大、验证器与用户反馈打架）+ 人工检查清单（`request_deep_review`），无 UI | — |

**三票裁决**（`feedback.three_vote_decision`）：验证器 + 用户 + 成本，两票才采纳。验证器 F2P/留出失败是一票否决；验证器弃权（单点 P2P 含糊）→ NEEDS_HUMAN；单个 👎 只暂停不杀死，连续两次才驳回；用户说好但成本超了也暂停。

**NEEDS_HUMAN 路由**（`selection.classify_p2p_failure` + 控制器；触发条件须同时满足）：

1. Tier A 通过；
2. Tier C 通过；
3. 恰好一个基线通过的测试新失败；
4. 若任务声明了 `behavior_tests`，该失败测试须在声明之列。

满足时不判 REJECT：保留 `rsi/<task-id>` 分支、写审批卡、账本记 NEEDS_HUMAN、基线不动。消解流程：人改测试期望（人工编辑 Morde 仓库，不走 Fixer）→ `rsi.py feedback --kind human_resolution --round <轮次> --note "update-test-expectation: ..."` → 下一轮重跑该任务。无消解记录时 `pick_next_task()` 跳过该任务，循环不会空转；消解记录按轮次绑定，旧轮次的消解不能重开新一轮的 NEEDS_HUMAN。≥2 个新失败测试、或 A/C 任一失败、或归因不上的，仍判 REJECT。无 per-test map 的 Portal 侧（vitest Plain 命令）用 Tier B 证据日志的 `FAIL <文件>` 行归因，且任务须声明 `behavior_tests`。

**`human_feedback.jsonl` / `telemetry.jsonl` 与 `reward.json` 的关系**：三者分开记。`human_feedback.jsonl` 是人点的/写的（5 个固定标签：`correct` 修对、`wrong` 修错、`misdirected` 修偏、`missed_point` 没修到点、`introduced_bug` 引入新问题；kind 有 `human_label`、`chat_reaction`、`human_resolution` 等）；`telemetry.jsonl` 是静默采集的行为信号（kind 有 `repeat_intent`、`manual_revert`、`approval_reject`/`approval_approve`、`delayed_recheck`、`shadow_compare`）。**反馈永不改写 `reward.json`**：反馈只影响三票裁决和下一轮的任务书（脱敏后），不直接改分。

**反馈转学习**（`feedback.feedback_to_learning`）：修错/没修到点/引入新问题 → 新留出测试**草案**（draft，人按它写真正的测试；草案本身不是测试、不计通过）；修偏 → 新 evolve 任务草案；修对或表述太含糊 → 只记权重。草案不自动写进任务文件、不自动计分。

**防泄漏**：Fixer 只看到脱敏的上一轮反馈（标签 + 一句话，经 `feedback_for_fixer`，无 evidence、无留出内容、本轮不可见），`critic.review_brief` 再检查一次；任务 JSON 新增可选字段 `behavior_change`（审批卡上的行为描述）和 `behavior_tests`（断言该行为的测试），见 `domains/morde/tasks/TEMPLATE.json`；CAND-001 已补上这两个字段（`behavior_tests: ["Projects.test.tsx"]`）。

**反馈机制的验证状态**：pytest 87/87 包含反馈模块的 37 个新增测试（三票裁决 10 组矩阵、反馈转学习、脱敏/泄漏、审批卡形状、NEEDS_HUMAN 路由含 vitest FAIL 行归因等）。但注意：**NEEDS_HUMAN 路由尚未在真实 CAND-001 上跑过**——`runs/morde/` 里现在没有一张真实落盘的 CAND-001 审批卡（`approval-CAND-001-c1.json` 不存在），`history.jsonl` 里也还没有 NEEDS_HUMAN 记录；`rsi.py status` 显示 `needs_human: []`、`feedback: {human: 0, telemetry: 0}`。等第 5 节①做完才会第一次真实触发。

---

## 4. 怎么跑

工作目录：`~/workspace/morde-rsi/`。以下命令除特别注明外，都是本文作者 2026-11-13 在这台机器上实跑通过的。

### 4.1 创建 venv、跑测试

```bash
cd ~/workspace/morde-rsi
python3 -m venv .venv
.venv/bin/pip install pytest          # 只装 pytest 就够跑测试
.venv/bin/python -m pytest -q         # 87 passed，约 25 秒
```

⚠️ **README 里写的 `.venv/bin/pip install -e ".[dev]"` 在当前环境跑不通**（本文作者在全新 venv 里实测失败）：setuptools 报错 `Multiple top-level packages discovered in a flat-layout: ['rsi', 'refs', 'runs', 'domains']`，因为仓库根目录有多个顶层包，自动发现拒绝继续。不要照抄这条命令；先用上面只装 pytest 的办法。修不修 `pyproject.toml`（加 `packages`/`find` 配置）由接手者定——这是个已知待修点，不是本文作者编造的。

已有 `.venv` 可直接用：`.venv/bin/python --version` 实测 Python 3.12.3。

### 4.2 存活检查与状态

```bash
.venv/bin/python rsi.py --domain morde smoke    # 实测 ok: true（go、node、npx、验证器、仓库、portal package.json 都在，evolve_tasks: 1）
.venv/bin/python rsi.py --domain morde status   # frontier + history + needs_human + feedback 计数；不碰仓库
.venv/bin/python rsi.py --help                  # 子命令一览
.venv/bin/python scripts/verify.py --help       # 验证器 CLI 用法（--worktree --task --out，0 当且仅当每层全过）
```

### 4.3 跑一轮（手动 Fixer，试点模式）

```bash
.venv/bin/python rsi.py --domain morde round --t 0 --fixer manual --diff <diff文件>
```

参数说明：`--t` 是轮次（0 起）；`--fixer manual` 必须配 `--diff <diff文件>`（人提供的 diff，账本标注 `fixer="manual-pilot"`，明确是模拟）；默认 `--fixer dsh` 目前是桩，见第 5 节③。正式跑（DSHFixer 接好之后）：

```bash
.venv/bin/python rsi.py --domain morde run
```

诚实说明：`round`/`run` 的命令形状来自 `rsi.py --help` 与 README，参数解析已验证（`rsi.py round --help` 通过）；**本文作者没有在本机实跑一轮真实试点**（实跑会启动真实轮次、改写账本），上一轮 CAND-001 是试点时跑的，证据在 `runs/morde/history.jsonl`。`runs/` 里有 `STOP` 文件时，`run` 会在当前轮结束后停表。

### 4.4 反馈命令：回答审批卡 / 消解 NEEDS_HUMAN

```bash
# 回答一张审批卡（human_label 必须配 --label）
.venv/bin/python rsi.py --domain morde feedback --task CAND-001 \
  --candidate CAND-001-c1 --kind human_label --label wrong \
  --note "修完还是显示已发送，路由状态没出来"

# 消解 NEEDS_HUMAN（人改完测试期望后，--round 必须与被卡住的轮次一致）
.venv/bin/python rsi.py --domain morde feedback --task CAND-001 \
  --candidate CAND-001-c1 --kind human_resolution --round 0 \
  --note "update-test-expectation: Projects.test.tsx:355 now expects '已分派任务'"
```

以上两条命令的格式已实测：`feedback` 子命令在 `--runs /tmp/rsi-handover-test` 的隔离目录里跑过，正常写盘一条 `human_resolution` 记录（`label: null`，`round_no: 0`），测试目录已清理，未污染 `runs/morde/`。注意：`feedback` 子命令不需要仓库、不需要 Fixer；`--label` 可选值是 5 个固定标签（`correct` / `wrong` / `misdirected` / `missed_point` / `introduced_bug`）。

---

## 5. 待办清单（按优先级）

### ① 用户待定：改 Projects.test.tsx:355 的期望后重跑 CAND-001，预期 ADOPT

- **在等谁**：用户决定。把 `Projects.test.tsx:355` 的断言期望从“已发送”改成“已分派任务”。
- **必须是人工改**：改测试文件本身是人做的事，**绝不让 Fixer 改**（critic 会判 GAMING-DETECTED）。改的是 Morde 仓库（`~/workspace/morde-check`）里的测试文件，改完不要顺手推 Morde 仓库（见第 6 节）。
- **重跑步骤**：人改完测试 → `rsi.py feedback --kind human_resolution --round 0 --note "update-test-expectation: ..."` 消解（命令见 4.4）→ 重跑 CAND-001（`round --t 1` 或 `run`）。预期：Tier A 通过、Tier B 529/529、Tier C 通过 → ADOPT，写 `frontier.json`，分支 `rsi/CAND-001`。
- **如果用户还没点头**：不要自己改测试、不要重跑。

### ② 跑 CAND-002

- 任务已备好：`domains/morde/tasks/CAND-002.evolve.json`（**草案，还没进 `evolve.json`**，激活办法是把这个任务对象追加进 `evolve.json` 的 `tasks` 数组，需用户说跑才做）。复现测试 `tasks/repro/CAND-002/model-select-title.test.tsx` 与留出测试 `tasks/heldout/CAND-002/model-select-title-heldout.test.tsx` 已备好，2026-11-13 对 morde-check HEAD 273d300 验证过：基线都失败（title 为 null），加一行 title 修复后都通过。
- 任务内容：601–920px 宽度下 AI 模型选择框被压扁截断。修复是给 `web/portal/src/routes/Assistant.tsx` 约 1342 行的模型 `<select>` 加 `title={当前选中项完整文案}`，约 1 行。注意：验证的是可观测契约（title 暴露全名），不是布局像素本身（jsdom 测不了截断像素）；如用户坚持修布局本身，tier A 改 Playwright e2e（更重）。证据与 tier C 思路见 `domains/morde/tasks/CANDIDATES.md` 候选 2。
- 候选池的诚实结论（CANDIDATES.md）：过硬的 A 级候选只有 CAND-001 和 CAND-002 两个，不是 5 个，不要凑数。第 3–5 个的正经办法：把 morde-check 更新到 origin/main 新 HEAD，或放宽标准（不推荐，会违反 A 级定义）。

### ③ 接 DSHFixer（现在是桩）

- 现状：`rsi/fixer.py` 的 `DSHFixer.fix()` 在产出任务书、过 `review_brief` 泄漏检查后，**抛 `NotImplementedError`**（本文作者实测确认：`DSHFixer is a stub: wire a DSH Task (see class docstring)`）。接线说明就在 `rsi/fixer.py` 的 `DSHFixer` 类文档字符串里，四步：① `render_brief(task, worktree, SKILL.md)` 产任务书；② 建 DSH Task，cwd 指向 Fixer worktree，模型用已配好的 DeepSeek/dsh（`domains/morde/rrsi.json` 里 `fixer_model: deepseek-v4-flash`）；③ 等任务完成（max_submissions、token/time 预算见 rrsi.json）；④ `git -C <worktree> diff HEAD` 取 diff，返回 `FixResult`，成本从 DSH Task 取。
- 约束：DSH 是 Morde 唯一的 Agent Runtime，不另起第二套；Fixer 工作区与 Morde 运行环境隔离，只改 git worktree 里的代码。
- 顺序建议：先跑完 ①②（手动 Fixer 验证循环），再接 DSHFixer 跑剩下的任务并量通过率（原计划：填 5 个真实小 bug，跑 1 个手动，剩下 4 个接 DSHFixer）。

### ④ 三个留接口（都还没做）

1. **影子对比喂输入适配器**：比较器 `feedback.shadow_compare` 是实的；跑基线版和候选版取输出的适配器还没接（以后接 Morde 运行时或测试驱动）。
2. **深度评审 UI**：只有触发条件 + 人工检查清单（`feedback.request_deep_review`），无 UI，人打开分支手工审。
3. **反馈草案自动进 evolve.json**：`feedback_to_learning` 现在只产草案（留出测试草案 / 新 evolve 任务草案），不自动写进任务文件、不自动计分；人决定是否采纳进 `evolve.json`。

其他未验证项：`playwright test` 仍未验证；Portal 侧 `node_modules` 在试点前需在主检出装好（2026-11-13 已在 `~/workspace/morde-check/web/portal` 跑过 `npm ci`，418 个包）；CO-0007 的两个待确认仍未定（小功能最终自动更新到哪里——草案建议先独立本地预览版，自动合入未定；“重大功能”的人工评审判定口径未定），这两件不挡试点 1–5，但挡第二阶段自动循环。

---

## 6. 硬规则（绝不能违反）

1. **Fixer 永远不改测试断言**。Fixer 碰测试文件/验证器/留出测试即 GAMING-DETECTED，该候选作废并记账。改测试期望只能人做（见 5-①）。
2. **不碰 Morde 仓库**：RSI 只读 Morde 的源码和测试，在自己的 worktree 里改。Morde 本地检出 `~/workspace/morde-check` 是 detached HEAD（`git rev-parse --abbrev-ref HEAD` 返回 `HEAD`），里面有 **12 个未提交的改动**（portal 改动，见第 7 节），**别动也别推**。RSI 的采纳只产生 `rsi/<task-id>` 分支，永不自动合并。
3. **Fixer 永远看不到留出测试和复现测试**：留出测试在工作区之外，由验证器注入、跑完删除；复现测试同样由验证器注入、跑完删除；任务书过 `review_brief` 泄漏检查。
4. **验证器必须是确定性脚本，不是 Agent**：不调任何外部模型，不把 Morde 源码发到任何外部。
5. **本地运行，不联网**：morde-rsi 不调外部模型；RRSI 的 LLM 评审已删掉。
6. **单条 👎 不杀死候选**：单个负反馈只暂停（NEEDS_HUMAN），连续两次才驳回。
7. **采纳要过噪声下限 delta=0.02**：`domains/morde/rrsi.json` 的 delta 是试点期初值（RRSI coding 实例是 0.017，由 calibrate 从基线 bootstrap 得来；morde-rsi 是按检查数拍脑袋的初值，**不是测出来的**），第一个真实试点跑完后用基线重复测量的方差重估计。
8. **模拟的都要明确标注为模拟**：测试与手动 Fixer 模式在账本里标 `fixer="manual-pilot"`，不能冒充 DSH 跑的结果。
9. **每 bug 最多 3 次提交**（`max_submissions: 3`）；token/时间预算见 `domains/morde/rrsi.json`。
10. **不要编造 bug**：进 `evolve.json` 的任务必须是真实（用户报过、或测试失败过、或亲手复现过）、小（A 级，一个函数内能说清）、可复现（先写出失败的测试或命令，基线跑一遍确认失败）。`heldout.json` 现在是空列表（试点期先用 evolve 任务的 tier C 做留出测量）。

---

## 7. 仓库与分支状态

- **morde-rsi 当时还不是 git 仓库**（本文作者 2026-11-13 核实：`git status` 报 `fatal: not a git repository`）。**不要假设它已经推上去了**——按上游要求，需要时再 `git init`、首次提交并推到指定远端，推之前先确认远端地址和分支名。
- `.gitignore` 已备好（仓库根），排除：`.venv/`、`__pycache__/`、`*.pyc`、`.pytest_cache/`、`refs/`、`runs/`、`*.egg-info/`。
- **`refs/rrsi/` 是 RRSI 参考克隆，不进仓库**（`.gitignore` 已排除；README 说需自行 `git clone https://github.com/google-research/rrsi.git refs/rrsi`，但本机 `refs/rrsi` 已存在——是 2026-11-13 拉的 `--depth 1` 参考版本）。
- **`runs/` 是本地证据，不进仓库**（`.gitignore` 已排除）。里面有：`runs/morde/history.jsonl`（1 条 CAND-001 REJECT 记录）、`runs/morde/evidence-CAND-001*`（三个证据目录：`evidence-CAND-001`、`evidence-CAND-001-base`、`evidence-CAND-001-base-direct`，各含 A-0/A-1/B-0/B-1/C-0.log）、`runs/pilot-readiness-2026-11-13.md`。无 `frontier.json`、无 `human_feedback.jsonl`、无 `telemetry.jsonl`（反馈账本还是空的，`status` 显示 human 0 / telemetry 0）。
- **Morde 仓库状态**（`~/workspace/morde-check`，本文作者 2026-11-13 核实）：detached HEAD，HEAD `273d300`（“导入iOS端”）。`git status --short` 有 12 个未提交改动，全部在 `web/portal/`：`M schema.d.ts`、`M app/api.test.ts`、`A app/k2-task-projection.test.ts`、`A app/k2-task-projection.ts`、`M mocks/handlers.test.ts`、`M mocks/handlers.ts`、`A mocks/k2-task-fixture.ts`、`M routes/Assistant.test.tsx`、`M routes/Assistant.tsx`、`M routes/Projects.test.tsx`、`M routes/Projects.tsx`、`M styles.css`。**这些改动不是 RSI 做的，RSI 没动过这个仓库；别动也别推。** 另注意：2026-11-12 日报提到的 Morde 新 HEAD 是 `6317715`，本地检出还停在旧的 `273d300`，试点基线必须对着新 HEAD 重新拉、重新量（见 `runs/pilot-readiness-2026-11-13.md`）。
- 分支：RSI 采纳的分支命名是 `rsi/<task-id>`（如 `rsi/CAND-001`）；目前没有任何 `rsi/*` 分支存在（CAND-001 是 REJECT，未建分支）。

---

## 8. 关键数字与证据位置

| 数字 | 值 | 证据位置 |
|---|---|---|
| pytest | **87/87 通过，约 25 秒**（本文作者实跑两次：25.76s、25.46s） | `tests/`（9 个文件，`def test_` 78 个，收集 87 项） |
| 骨架初验 | 47/47 通过，14.7 秒 | 2026-11-13 验收记录（`~/MEMORY.md`） |
| CAND-001 试点后 | 50/50 | 同上 |
| CAND-001 基线 score | 0.6（A 失败、C 失败、B 529/529） | `runs/morde/history.jsonl`、`runs/morde/evidence-CAND-001-base*` |
| CAND-001 候选 score | 0.8（A 通过、C 3/3、B 528/529） | `runs/morde/history.jsonl`、`runs/morde/evidence-CAND-001/` |
| 唯一失败测试 | `Projects.test.tsx:355`（期望“已发送”，应为“已分派任务”） | `runs/morde/evidence-CAND-001/B-*.log` |
| 噪声下限 | delta=0.02（试点初值，未实测重估） | `domains/morde/rrsi.json` |
| Morde 基线失败测试 | worker 包 46 个（43 握手 + 3 浏览器超时）、app 包 1 个 | `docs/rrsi-mapping.md`“基线发现”一节 |
| Portal 基线 | 51 文件 / 529 测试全过；`npm ci` 418 个包，约 6 秒 | `docs/rrsi-mapping.md`、CANDIDATES.md |
| 反馈模块 | `rsi/feedback.py` 669 行；新增测试 37 个 | `rsi/feedback.py`、`tests/test_feedback.py` |
| 账本 | 1 条记录（CAND-001 REJECT，2026-11-13 01:19:11） | `runs/morde/history.jsonl` |
| 任务 | evolve.json 有 1 个真实任务（CAND-001）；heldout.json 为空列表；CAND-002 是草案文件，未进 evolve.json | `domains/morde/tasks/evolve.json`、`heldout.json`、`CAND-002.evolve.json` |
| Morde 本地检出 | HEAD `273d300`，detached，12 个未提交改动 | `~/workspace/morde-check` |

### 读文档的顺序建议

1. 本文件（全貌）→ 2. `README.md`（架构与怎么跑）→ 3. `docs/rrsi-mapping.md`（RRSI 对照、基线发现、CAND-001 试点全记录、反馈机制全记录）→ 4. `domains/morde/README.md`（任务怎么填、环境要求、守卫）→ 5. `domains/morde/tasks/CANDIDATES.md`（候选池、为什么只有 2 个过硬候选）→ 6. `rsi/controller.py`、`rsi/feedback.py` 的文件头注释（循环与反馈的设计原意）。

### 背景出处

- 需求草案 CO-0007（2026-11-12，用户上传，draft，未实现）：Morde 小功能自动开发与 RSI 自循环优化；两阶段（小功能自动开发 → RSI 自循环）；RSI 是独立项目；两个待确认未定（最终自动更新到哪里；“重大功能”判定口径）。
- RSI 深度研究（2026-11-12，页面“RSI 深度研究”）：fixer → 独立确定性验证器在全新隔离副本里打分 → 只有比基线好过噪声才采纳；最值得借的是 RRSI（git worktree + 证据账本 + 噪声下限 + 防泄漏 critic）和 OpenRSI 的 Work/Judge 分离。
- 建造决定（2026-11-13，用户确认）：RRSI 当骨架、DSH 当执行器、不 fork 任何整项目。
