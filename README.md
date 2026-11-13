# morde-rsi

Morde 的 RSI（递归自我改进）项目——**独立项目，不放进 Morde**。

目标：Morde 自己发现 bug、修掉、并**证明**修对了，越修越好；修坏的进不来；
用户随时能停；重大功能人审。

```
发现器 → 裁判长 → Fixer（DSH Task）→ Verifier（Judge，确定性脚本）→ 证据账本
              ↑              ↓                         ↓
              └────── 采纳 / 拒绝 / 卡住 / 作弊 ──────────┘
```

## 架构

```
┌────────────────────────────────────────────────────────────────┐
│  morde-rsi（独立仓库，本地运行，不调外部模型）                    │
│                                                                │
│  rsi/controller.py   裁判长：DISCOVER → BASELINE → FIX →       │
│                      VERIFY → ADOPT/REJECT → NEXT；预算、STOP   │
│                      文件、runs/ 可续跑                         │
│  rsi/fixer.py        Fixer 接口。DSHFixer 是桩（以后接 DSH Task）；│
│                      ManualFixer 是试点用的人工模式（账本标注）  │
│  rsi/verifier.py     Judge：确定性脚本。A 构建+指定单测 → B 回归  │
│                      → C 注入留出测试。输出 reward.json          │
│  rsi/critic.py       评估前防泄漏：路径黑名单 + 凭据正则；        │
│                      命中受保护路径即 GAMING-DETECTED            │
│  rsi/selection.py    算法 2：噪声下限 + 成本规则 + F2P/P2P 硬门；  │
│                      classify_p2p_failure（单点失败走 NEEDS_HUMAN）│
│  rsi/schedule.py     退火编辑预算（RRSI 公式）                   │
│  rsi/worktree.py     git worktree：Fixer 工作区 / 干净 Judge     │
│                      工作区 / 清理                              │
│  domains/morde/      morde domain：超参数、提议者宪法、任务列表、 │
│                      接 Morde 测试的适配器                       │
└────────────────────────────────────────────────────────────────┘
        ▲ 读源码/测试（只读）   │ 候选分支 rsi/<task-id>（不自动合并）
┌────────────────────────────────────────────────────────────────┐
│  Morde 仓库（lizhongxuan/morde，本地）                            │
│  Go 服务端 go build/vet/test；Portal tsc + vitest；Playwright e2e │
└────────────────────────────────────────────────────────────────┘
```

## 和 RRSI 的关系

骨架来自 RRSI（`google-research/rrsi`，Apache 2.0，参考副本在 `refs/rrsi/`，
需自行 `git clone https://github.com/google-research/rrsi.git refs/rrsi`）。
**没有 fork、没有整体复制代码**：核心模块是按 RRSI 的算法重写的轻量版，
对照见 `docs/rrsi-mapping.md`。

借的东西：worktree 隔离、JSONL 账本、leakage critic、噪声下限、成本规则、
退火预算、domain 适配器。
改的东西：评估对象从"harness 在基准任务上的表现"改为"候选 diff 修 Morde
bug 的效果"；删掉 LLM 评审（本地优先，不调外部模型）；采纳只提交到
`rsi/<task-id>` 分支，永不自动合并；Fixer 是 DSH Task（Morde 唯一的
Agent Runtime，见 Morde 仓库 `AGENTS.md`）。

## 哪些是桩

| 桩 | 现状 | 接入条件 |
|---|---|---|
| `DSHFixer`（`rsi/fixer.py`） | 桩：`fix()` 抛 `NotImplementedError`，但 `render_brief()` 已可产出 DSH Task 任务书 | DSH Task 能接 worktree 路径 + 任务书、返回 diff；模型走已有的 DeepSeek/dsh |
| `domains/morde/tasks/evolve.json` / `heldout.json` | 只有结构模板和填写说明，**0 个真实任务** | 试点时填 5 个真实小 bug（见 `domains/morde/README.md`） |
| 发现器 | 桩：`controller.pick_next_task()` 按 evolve.json 顺序取下一个未尝试任务 | 以后再接自动发现（失败测试、用户反馈、lint） |
| 分析器（RRSI 的 analyst/digester） | 没有：试点期人看账本 + reward.json | 以后再说 |
| 影子对比的输入喂给 | 比较器（`feedback.shadow_compare`）是实的；跑基线版和候选版取输出的适配器还没接 | 以后接 Morde 运行时或测试驱动 |
| 深度评审（3 层） | 桩：只有触发条件 + 人工检查清单（`feedback.request_deep_review`），无 UI | 人打开分支 + 证据手工审 |

## 怎么跑

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q            # 测试全部通过才算骨架就绪

# 冒烟：验证器脚本对微型 fixture 跑一轮（不碰 Morde）
.venv/bin/python scripts/verify.py --worktree <fixture> --task <task.json> --out reward.json

# 一轮模拟循环（手动 Fixer，明确标注为模拟）
.venv/bin/python rsi.py --domain morde round --t 0 --fixer manual --diff <diff文件>

# 正式跑（DSHFixer 接好之后）
.venv/bin/python rsi.py --domain morde run
.venv/bin/python rsi.py --domain morde status

# 人工反馈：回答一张审批卡，或消解一个 NEEDS_HUMAN（见下“反馈机制”）
.venv/bin/python rsi.py --domain morde feedback --task CAND-001 \
  --candidate CAND-001-c1 --kind human_label --label wrong \
  --note "修完还是显示已发送，路由状态没出来"
```

`runs/` 里有 `STOP` 文件时，`run` 会在当前轮结束后停表。`runs/` 可续跑：
`frontier.json` + `history.jsonl` 就是全部状态。

## 验证器三层

- **A**：构建 + vet + 指定单元测试（任务 JSON 里写）。复现测试若不在仓库
  里，由验证器从任务目录注入（`tiers.A.repro`），跑完删除，Fixer 看不到。
  不过则 B、C 跳过（fail-fast；基线跑全量以捕获逐测试基线图）。
- **B**：回归。`go test -json` + `mode: suite-ran`：套件跑完即算通过，
  逐个测试的结果记进 reward，由裁判长按基线逐测试对比（P2P）。
  **绝不能是"整包必须通过"**：Morde 基线本身就有 46 个环境依赖的失败测试
  （DSH 握手、浏览器超时，见 `docs/rrsi-mapping.md`），整包门会让任何候选
  都过不了。基线通过的测试修完还得通过；基线已失败的不计入 P2P。
- **C**：验证器把留出测试从**工作区之外的**留出目录注入 worktree，跑完删除。
  Fixer 永远看不到留出测试；注入路径对 Fixer 只读。留出测试必须全过
  （all-pass）；tier C 的命令只跑留出文件，不要跑整包。
- worktree 里的 Portal 依赖：`git worktree add` 不带 gitignored 的
  node_modules，裁判长会在每个 Judge worktree 里实打实跑一次 `npm ci`
  （`rsi/worktree.py::provision_node_modules`，约 6 秒）。软链不行：Vite
  的 module runner 会拒绝真实路径在项目根之外的文件。

输出 `reward.json`：每层通过/失败、每条检查的证据路径、耗时、分数、
逐测试结果（`go-test-json` 时）。

## 反馈机制（2026-11-13 新增，`rsi/feedback.py`）

反馈必须在 Fixer 外面，Fixer 看不到。分三层，越往上越少出现：

- **1 层静默遥测（无感知）**：纯函数，从行为里采信号，不弹窗——同一意图
  重提（`detect_repeat_intent`）、用户手动改回 RSI 改过的文件
  （`detect_manual_revert`，强负反馈）、审批卡驳回/通过、延迟复核
  （采纳后 24h/7d 在新 worktree 重跑验证器，`Controller.delayed_recheck`）、
  影子对比（同样输入跑两版比输出，`shadow_compare` 是比较器，喂输入的
  适配器是桩）。记在 `runs/<domain>/telemetry.jsonl`，与 `reward.json` 分开，
  不覆盖 reward。
- **2 层一键审批卡**：复用 Morde 审批卡格式，不新发明 UI。卡上只有行为变化
  （不是 diff——`build_approval_card` 会拒收 diff 文本）、测试通过数、三层
  结果、成本、分数前后；动作最多三个：采纳 / 驳回 / 修偏了。成功默认静默
  进预览不问；不确定、修偏、成本超了才问；可攒一周批量问
  （`batch_approval_cards`）。对话里的 👍/👎 只记第一次
  （`record_chat_reaction`）。卡以 `runs/<domain>/approval-<task>-c1.json`
  落盘。
- **3 层深度评审**：桩，只有触发条件（重大功能、改动面大、验证器与用户反馈
  打架）+ 人工检查清单（`request_deep_review`），无 UI，人打开分支手工审。

**裁决（三票）**：验证器 + 用户 + 成本，两票才采纳
（`three_vote_decision`）。验证器 F2P/留出失败是一票否决；验证器弃权
（单点 P2P 含糊）进 NEEDS_HUMAN；单个 👎 只暂停不杀死，连续两次才驳回。

**NEEDS_HUMAN 路由（CAND-001 的教训）**：Tier A、C 全过、且只有一个
基线通过的测试新失败时，裁决不得自动判 REJECT——测试可能把有 bug 的行为
写成了期望（CAND-001 的 `Projects.test.tsx:355` 就是）。此时保留分支
`rsi/<task-id>`、写审批卡、账本记 NEEDS_HUMAN、基线不动；人改测试期望
（人工编辑，不走 Fixer）后用 `rsi.py feedback --kind human_resolution
--round <轮次> --note "update-test-expectation: ..."` 消解，任务才可重跑。
无 per-test map 的 Portal 侧（vitest）用 tier B 证据日志的 `FAIL <文件>` 行
归因，且任务须在 `behavior_tests` 里声明断言该行为的测试；归因不上的仍判
REJECT。`behavior_change`（审批卡上的行为描述）和 `behavior_tests` 是任务
JSON 的可选字段，见 `domains/morde/tasks/TEMPLATE.json`。

**反馈转学习**（`feedback_to_learning`）：每条反馈须能转成可执行物——新
留出测试 *草案*（draft，人按它写真正的测试；草案本身不是测试、不计通过）
或新 evolve 任务草案；转不成的只记权重，不直接改分。

**防泄漏**：Fixer 只看到脱敏的上一轮反馈（标签 + 一句话，`feedback_for_fixer`，
无 evidence、无留出内容、本轮不可见）；`critic.review_brief` 检查任务书
是否带留出标记；diff 碰反馈账本/审批卡/`rsi/feedback.py` 即 GAMING-DETECTED。

## 安全边界

- 不修改 Morde 仓库：只读 Morde 的源码和测试，在自己的 worktree 里改。
- 不把 Morde 源码发到任何外部；不调用任何外部模型。
- 不自动合并：采纳只产生 `rsi/<task-id>` 分支，合入主分支是人工审 PR 的事。
- 验证器相关路径对 Fixer 只读；Fixer 改测试/验证器/留出测试即
  `GAMING-DETECTED`，该候选作废并记账。
- 每 bug 最多 3 次提交；token/时间预算见 `domains/morde/rrsi.json`。
- 模拟的都要明确标注为模拟（测试与手动 Fixer 模式）。

## 许可证

Apache-2.0（与 RRSI 一致），见 `LICENSE`。
