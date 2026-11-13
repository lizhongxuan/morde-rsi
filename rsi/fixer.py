"""Fixer interface.

The Fixer is the only agent in the loop. It works in its own isolated
worktree, sees only the task brief + allowlisted paths, never sees held-out
tests, and cannot touch the verifier. Two implementations:

* DSHFixer (STUB): the real fixer. Wiring it is a separate step: a DSH Task
  (Morde's only Agent Runtime) is created with the brief rendered by
  render_brief(), pointed at the worktree, and the resulting diff is collected
  with `git -C <worktree> diff HEAD`. Until that wiring exists, fix() raises
  NotImplementedError with the wiring instructions.
* ManualFixer: pilot mode. A human supplies a diff file; the fixer applies it
  to the worktree. Every ledger record for this mode carries
  fixer="manual-pilot" so a simulated run can never be mistaken for a DSH run.
"""
from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


from . import critic as critic_mod


@dataclass
class FixResult:
    diff: str
    submissions: int = 1
    cost_tokens: int | None = None
    cost_seconds: float = 0.0
    detail: str = ""


class Fixer(ABC):
    name: str = "fixer"

    @abstractmethod
    def fix(self, task: dict, worktree: Path, budget: dict) -> FixResult:
        """Produce a diff for `task` inside `worktree`, within `budget`."""


def render_brief(task: dict, worktree: Path, skill_md: str,
                 max_submissions: int = 3,
                 prior_feedback: list[dict] | None = None) -> str:
    """Render the DSH Task brief for one bug-fix task.

    This is the text the DSH Task receives. The task's allowlist and
    constraints come from domains/morde/SKILL.md; the task JSON supplies the
    bug description, the repro command and the tier-A checks.

    `prior_feedback` is the sanctioned feedback channel: sanitised
    (label + one-line note only), prior rounds only, never any held-out
    content -- see rsi/feedback.py::feedback_for_fixer. It is listed for
    context only; the Fixer must not edit tests or the verifier to satisfy
    it.
    """
    feedback_section = ""
    if prior_feedback:
        lines = "\n".join(
            f"- [{f.get('label') or '无标签'}] {f.get('note', '')}"
            for f in prior_feedback)
        feedback_section = (
            "\n## 上一轮反馈（已脱敏，仅供参考）\n"
            "这些是上一轮的外部反馈，不是本轮的评判标准；不要为了迎合它们"
            "去改测试或验证器。\n" + lines + "\n")
    return f"""# 修复任务 {task.get('task_id', '?')}

工作区：{worktree}（隔离 worktree，只改这里，不要碰其他目录）

## Bug
{task.get('description', '(无描述)')}

## 复现
{task.get('repro', '(无复现步骤)')}
{feedback_section}
## 规则（见 SKILL.md 全文，摘要如下）
{skill_md}

## 提交上限
最多 {max_submissions} 次提交。改完后在工作区里跑 tier A 的命令自查，
通过再提交 diff。不要碰测试文件、不要碰验证器、不要碰留出测试。
"""


class DSHFixer(Fixer):
    """STUB. Real fixer: a DSH Task (Morde's Agent Runtime).

    Wiring (not yet implemented):
      1. Render the brief with render_brief(task, worktree, SKILL.md).
      2. Create a DSH Task with that brief, cwd = the fixer worktree,
         model = the configured DSH model (DeepSeek via dsh, already running
         for morde-muse). The task may only edit allowlisted paths.
      3. Wait for the task to finish (max_submissions, token/time budgets
         from domains/morde/rrsi.json).
      4. Collect the diff with `git -C <worktree> diff HEAD` and return it
         as FixResult, with cost_tokens/cost_seconds from the DSH Task.
    """
    name = "dsh"

    def __init__(self, model: str = "deepseek-v4-flash", skill_md: str = ""):
        self.model = model
        self.skill_md = skill_md

    def fix(self, task: dict, worktree: Path, budget: dict) -> FixResult:
        brief = render_brief(
            task, worktree, self.skill_md, budget.get("max_submissions", 3),
            prior_feedback=task.get("prior_feedback"))
        # Wiring guard: the brief must never carry held-out material. A
        # hit is a bug in the caller's wiring, not fixer gaming.
        check = critic_mod.review_brief(
            brief, heldout_markers=task.get("heldout_markers") or [])
        if check["verdict"] == "reject":
            raise RuntimeError(
                "brief leaked verifier material (wiring bug, not a fix): "
                + "; ".join(check["reasons"]))
        raise NotImplementedError(
            "DSHFixer is a stub: wire a DSH Task (see class docstring). "
            "Brief for this task:\n" + brief)


class ManualFixer(Fixer):
    """Pilot/manual mode: a human supplies the diff. SIMULATED, not a DSH run.

    The diff is applied to the worktree with `git apply`. Use only for the
    pilot (proving the loop mechanics) and for replaying known bugs.
    """
    name = "manual-pilot"

    def __init__(self, diff_file: Path | str):
        self.diff_file = Path(diff_file)

    def fix(self, task: dict, worktree: Path, budget: dict) -> FixResult:
        t0 = time.time()
        diff = self.diff_file.read_text()
        if not diff.strip():
            return FixResult(diff="", submissions=1,
                             cost_seconds=round(time.time() - t0, 1),
                             detail="manual-pilot: empty diff file")
        r = subprocess.run(["git", "-C", str(worktree), "apply",
                            "--whitespace=nowarn"],
                           input=diff, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"manual-pilot: git apply failed: {r.stderr.strip()}")
        return FixResult(diff=diff, submissions=1,
                         cost_seconds=round(time.time() - t0, 1),
                         detail="manual-pilot: diff applied by a human (SIMULATED run)")
