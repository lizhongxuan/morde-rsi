# self domain

RSI 循环里"本项目自己"这一端的适配器：任务列表、超参数、提议者宪法、
验证命令、守卫规则。循环目标从 Morde 换成本项目自己：Fixer 改本项目
源码，验证器跑本项目 pytest + 冒烟。

## 任务怎么填

`tasks/evolve.json` 是要修的本项目小 bug 列表，`tasks/heldout.json`
是留出集（试点期为空，先用 evolve 任务的 tier C 做留出测量）。

**不要编造 bug。** 挑法与 morde domain 相同：

1. 真实：用户报过、或测试失败过、或你亲手复现过的；
2. 小：A 级（见 `SKILL.md`），一个函数内能说清；
3. 可复现：先写出失败的测试或命令，基线跑一遍确认它失败——
   复现不了的不要进列表。

形状见 `tasks/TEMPLATE.json`。每个任务必须带：

- `task_id`（如 `SELF-0001`）、`title`、`description`、`repro`
- `component`、`hypothesis`
- `tiers.A`：复现测试（**必须包含复现 bug 的那条测试**，基线要先失败，
  修完要通过——这就是 F2P）。复现测试由验证器从
  `tasks/repro/<task-id>/` 注入 worktree 的 `tests/`，跑完删除。
- `tiers.B`：回归——本项目 pytest 全量（`.venv/bin/python -m pytest -q`，
  用仓库自己的 venv python，见下"验证命令"）。**全量必须通过**：本项目
  基线 pytest 是全绿的，不像 Morde 基线有环境失败测试，所以 tier B
  是整包门，不是逐测试 P2P。
- `tiers.C`：留出测试命令 + `heldout.src`（留出文件目录，在工作区之外，
  如 `tasks/heldout/SELF-0001`）+ `heldout.dest`（注入进 worktree 的位置，
  如 `tests`）。验证器跑完删除注入的文件。

可选字段（反馈机制用，与 morde domain 相同）：

- `behavior_change`：修完后用户能观察到的行为变化（一句话，不写代码改动；
  审批卡上就显示这句）。
- `behavior_tests`：断言上述行为的测试文件/测试名。单点 P2P 失败时，若未过
  的测试正在此列，裁决走 NEEDS_HUMAN 而不是自动 REJECT。

## 验证命令

验证器跑的是**本项目自己的 pytest**，不要调 Morde 的 go test/vitest：

- tier A：`.venv/bin/python -m pytest -q tests/<复现测试文件>`（注入的
  复现测试）
- tier B：`.venv/bin/python -m pytest -q`（全量）
- tier C：`.venv/bin/python -m pytest -q tests/<留出测试文件>`（注入的
  留出测试）

任务 JSON 里写的是绝对路径 `/home/hatch/workspace/morde-rsi/.venv/bin/python`：
Judge worktree 是全新的 git 检出，没有 `.venv`（gitignored），而系统
python3 没有 pytest，所以解释器必须从 worktree 之外来——与 morde
domain 的 `rrsi.json` 写绝对 `repo_path` 是同一个做法。仓库搬家时，
`adapter.py` 的 `VENV_PYTHON` 与 `tasks/evolve.json` 里的路径要一起改。

复现/留出测试本身用 `sys.executable -m venv` 建全新 venv（跑测试的
解释器就是仓库 venv python），再在里面跑 `pip install`。建 venv 与
pip 装包需要联网（pip 要从 PyPI 取 setuptools 构建后端与 dev 依赖）。

## 反馈怎么记

与 morde domain 相同，账本在 `runs/self/`：

- 审批卡回答 / 消解 NEEDS_HUMAN：`python rsi.py --domain self feedback
  --task <id> --candidate <id>-c1 --kind human_label --label wrong
  --note "一句话：发生了什么"`；消解用 `--kind human_resolution --round <轮次>
  --note "update-test-expectation: ..."`，任务下一轮才可重跑。
- 反馈记在 `runs/self/human_feedback.jsonl`（人点的/写的）和
  `runs/self/telemetry.jsonl`（静默采集的行为信号），与 `history.jsonl`、
  `reward.json` 分开，永不改写 reward。

## 环境要求（跑 self 轮次前必须先跑通）

- 本仓库 `.venv` 已建好且装了 pytest（只装 pytest 就够跑测试）
- 仓库在 `rrsi.json` 的 `repo_path`（可用 `--repo` 覆盖）

检查：`python rsi.py --domain self smoke`。它只做存活检查，不跑测试。

## 元护栏（最重要）

自我进化时循环绝不能给自己改卷。两道门，用的是同一份路径表
（`rsi/critic.py` 的 `SELF_PROTECTED_PATH_PATTERNS` 与本适配器的
`CRITIC_PATTERNS` / `guards_for_diff`）：

| 门 | 触发 | 裁决 |
|---|---|---|
| 1. critic（`rsi/critic.py::review(domain="self")`） | diff 碰保护路径 | GAMING-DETECTED |
| 2. 守卫（`adapter.guards_for_diff`，经 `rsi.py` 的 guard_fn） | diff 碰保护路径 | 不可采纳（REJECT） |

保护路径：`rsi/verifier.py`、`rsi/critic.py`、`rsi/selection.py`
（采纳阈值）、`rsi/feedback.py`（防泄漏规则）、`rsi/analyzer.py`、
`rsi/history.py` 等 `rsi/` 全部、`rsi.py`、测
试 `tests/` 全部、`domains/self/rrsi.json`（阈值）、
`domains/self/SKILL.md`（提议者宪法）、`domains/self/adapter.py`、
`domains/self/tasks/` 全部（任务定义、复现、留出）、`runs/` 全部
（账本与 reward）。改动这些只能走人工 PR，必要时走 NEEDS_HUMAN。

**注意第 1 道门今天还没接上线**：控制器（`rsi/controller.py`）目前
调 `critic.review(diff, domain_patterns=None)`，不传 domain，所以
self 的黑名单在真实轮次里还不会触发 GAMING-DETECTED；第 2 道门
（守卫）已经接线，会先拦住（REJECT）。接线缺口见汇报，协调人处理。

**结构性弱点（已记录，不是漏洞假装不存在）**：考题与考生同仓——
`domains/self/tasks/repro/` 与 `heldout/` 是仓库的受版本控制文件，
Fixer 的 worktree 里就有它们，Fixer 能读到考题。当前缓解：SKILL.md
明文禁读考题；critic 对任何改动判 GAMING-DETECTED；任务书不含考题
内容；留出测试断言的是通用性质（装包后能导入），不是可特判的值；
采纳前人工审 PR。彻底隔离需要控制器在建 Fixer worktree 时排除
`tasks/` 目录（接口缺口，见汇报）。

## 守卫与白名单的关系

- 白名单（SKILL.md）：Fixer 只许碰 `pyproject.toml`（试点期）。
- 黑名单（critic + 守卫）：碰保护路径直接判作弊 / 不可采纳。
- 白名单之外的路径，即使不在黑名单里，Fixer 也不许碰——白名单就是
  全部活动范围。两道门一致：白名单之外的改动要么被黑名单抓住，要么
  在人工审 PR 时被打回。

## 怎么跑

```bash
# 冒烟：存活检查，不跑测试
python rsi.py --domain self smoke

# 一轮（手动 Fixer，模拟，账本标注 manual-pilot）
python rsi.py --domain self round --t 0 --fixer manual --diff <diff文件>

# 正式跑（DSHFixer 接好之后，默认就是 DSH）
python rsi.py --domain self run
python rsi.py --domain self status
```

## 分析器

`rsi/analyzer.py` 是确定性分析器（不用 LLM）：读 `runs/self/history.jsonl`
+ `runs/self/telemetry.jsonl`，出一份小报告（失败模式聚类、组件胜率、
抖动测试、草案）。`report_markdown()` 渲染成 markdown，
`write_report(path)` 落盘；草案复用 `feedback.feedback_to_learning`
的路子，只产草案，不自动写进 `evolve.json`。
