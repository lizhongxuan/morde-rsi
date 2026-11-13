# morde domain

RSI 循环里"Morde"这一端的适配器：任务列表、超参数、提议者宪法、
验证命令、守卫规则。

## 任务怎么填

`tasks/evolve.json` 是试点要修的 bug 列表，`tasks/heldout.json` 是留出集
（试点期为空，先用 evolve 任务的 tier C 做留出测量）。

**现在是 0 个真实任务，不要编造 bug。** 试点时填 5 个真实小 bug，
挑法：

1. 真实：用户报过、或测试失败过、或你亲手复现过的；
2. 小：A 级（见 `SKILL.md`），一个函数内能说清；
3. 可复现：先写出失败的测试或命令，基线跑一遍确认它失败——
   复现不了的不要进列表。

形状见 `tasks/TEMPLATE.json`。每个任务必须带：

- `task_id`（如 `MORDE-0001`）、`title`、`description`、`repro`
- `component`、`hypothesis`
- `tiers.A`：构建 + vet + 指定单元测试（**必须包含复现 bug 的那条测试**，
  基线要先失败，修完要通过——这就是 F2P）
- `tiers.B`：回归（服务端 `go test ./...`、Portal `typecheck` + `vitest run`）
- `tiers.C`：留出测试命令 + `heldout.src`（留出文件目录，在工作区之外，
  如 `tasks/heldout/MORDE-0001`）+ `heldout.dest`（注入进 worktree 的位置，
  如 `web/portal/e2e`）。验证器跑完删除注入的文件。

可选字段（反馈机制用，2026-11-13 新增）：

- `behavior_change`：修完后用户能观察到的行为变化（一句话，不写代码改动；
  审批卡上就显示这句）。
- `behavior_tests`：断言上述行为的测试文件/测试名。单点 P2P 失败时，若未过
  的测试正在此列，裁决走 NEEDS_HUMAN 而不是自动 REJECT（CAND-001 的教训：
  测试可能把有 bug 的行为写成了期望）。

## 反馈怎么记

- 审批卡回答 / 消解 NEEDS_HUMAN：`python rsi.py --domain morde feedback
  --task <id> --candidate <id>-c1 --kind human_label --label wrong
  --note "一句话：发生了什么"`；消解用 `--kind human_resolution --round <轮次>
  --note "update-test-expectation: ..."`，任务下一轮才可重跑。
- 反馈记在 `runs/morde/human_feedback.jsonl`（人点的/写的）和
  `runs/morde/telemetry.jsonl`（静默采集的行为信号），与 `history.jsonl`、
  `reward.json` 分开，永不改写 reward。详见仓库 README 的"反馈机制"一节。

## 环境要求（试点前必须先跑通）

- Go 工具链（`server/go.mod` 要求 go 1.25.0，toolchain go1.27.1）
- Node 24 + `web/portal/node_modules` 已装（含 Playwright + Chromium/Chrome）
- Morde 仓库在 `rrsi.json` 的 `repo_path`（可用 `--repo` 覆盖）

检查：`python rsi.py --domain morde smoke`。它只做存活检查，不跑测试。

**注意**：Go 侧命令已在 2026-11-13 真实验证（`/tmp` 克隆，HEAD 273d300）：
`go build ./...`、`go vet`、指定单测全过；`go test -json` 逐测试解析也跑通
（worker 包 345 个测试结果、app 包 1328 个）。但基线本身有环境依赖的失败
测试（worker 46 个、app 1 个，见 `docs/rrsi-mapping.md`），所以 tier B
必须是逐测试 P2P，不能是整包门。Portal 侧已在第一轮试点（CAND-001，
2026-11-13）验证：Judge worktree 里 `npm ci` 实装依赖后，
`npm run typecheck` + `npx vitest run` 全过（51 文件 / 529 测试，基线）；
`playwright test` 仍未验证。

## 怎么跑

```bash
# 一轮（手动 Fixer，模拟，账本标注 manual-pilot）
python rsi.py --domain morde round --t 0 --fixer manual --diff <diff文件>

# 正式跑（DSHFixer 接好之后，默认就是 DSH）
python rsi.py --domain morde run
python rsi.py --domain morde status
```

## 守卫（pilot）

`adapter.py` 的 `guards_for_diff()`：diff 碰了以下路径直接不可采纳——
审批卡（`server/internal/app/agent_tools.go`）、协调器
（`server/internal/worker/cloud_dsh_coordinator.go`）、数据库迁移、
凭据/密钥文件、DSH 版本 pin。这是 critic 的路径黑名单之外的第二道门：
critic 管"作弊"，守卫管"试点期不该动的范围"。
