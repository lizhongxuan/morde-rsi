"""Self domain adapter: the RSI loop pointed at its own repo.

RRSI-shaped Domain (cf. domains/morde/adapter.py): the same interface
(evolve_ids / heldout_ids / run / score / smoke / critic_patterns /
guards), but the target repo is morde-rsi itself and the evaluation
backend is this project's own pytest suite instead of Morde's go test /
vitest. The task JSONs name the repo's own venv python by absolute path
(VENV_PYTHON below): a Judge worktree is a fresh git checkout with no
.venv, and the system python3 has no pytest, so the interpreter must come
from outside the worktree -- the same reason domains/morde/rrsi.json
carries an absolute repo_path.

NOTE on the interface (see README.md): the controller does NOT call
domain.run()/domain.score() today -- it inlines run_verifier() and the
selection calls. run()/score() are implemented here for interface parity
with the morde domain (and so `rsi.py --domain self` smoke/status work),
but a real self round executes the task JSON's tier commands through the
controller -> run_verifier path. The controller DOES pass domain="self"
to critic.review() (first gate, GAMING-DETECTED) and guards_for_diff()
(wired via rsi.py's guard_fn) is the second gate (REJECT); both use the
same protected list. The controller also removes the task material from
the Fixer worktree (fixer_worktree_excludes below) so the Fixer cannot
read its own exam -- the exclusion is a third, physical layer, not a
substitute for the two gates.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from rsi import selection as sel  # noqa: E402
from rsi.verifier import run_verifier  # noqa: E402

CFG = json.loads((HERE / "rrsi.json").read_text())

# The interpreter the task JSONs run pytest with. A Judge worktree has no
# .venv (gitignored) and system python3 has no pytest, so this must be
# the main checkout's venv python, by absolute path. If the repo moves,
# update this and the matching argv paths in tasks/evolve.json together.
VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")

# Component vocabulary for the proposer (see SKILL.md). Self fixes are
# packaging/config/docs small fixes; the vocabulary stays small on purpose.
COMPONENTS = ["config-default", "logic-small", "docs"]

# Paths removed from the Fixer worktree before it starts (and excluded
# from its diff): the task material itself. The Fixer gets the task
# through the brief, never by reading these files -- the exam must not
# be readable from inside the room where it is taken.
FIXER_WORKTREE_EXCLUDES = ["domains/self/tasks"]
fixer_worktree_excludes = FIXER_WORKTREE_EXCLUDES

# Domain critic patterns, applied to changed file paths in the diff
# (in addition to rsi/critic.py's generic protected paths). These are the
# same patterns as the critic's self blacklist: the Fixer must never
# rewrite its own judge, threshold, task, or ledger.
CRITIC_PATTERNS = [
    (r"(^|/)rsi\.py$", "self-loop CLI entry touched"),
    (r"(^|/)tests/", "test directory touched (self domain)"),
    (r"(^|/)runs/", "runs ledger/evidence directory touched (self domain)"),
    (r"history\.jsonl$", "evidence ledger touched (self domain)"),
    (r"(^|/)domains/self/rrsi\.json$", "self-loop thresholds touched"),
    (r"(^|/)domains/self/SKILL\.md$", "self proposer constitution touched"),
    (r"(^|/)domains/self/adapter\.py$", "self domain adapter touched"),
    (r"(^|/)domains/self/tasks/", "self task material touched"),
]


def _load_tasks(fname: str) -> list[dict]:
    p = HERE / "tasks" / fname
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return data.get("tasks", []) if isinstance(data, dict) else data


def guards_for_diff(diff: str) -> list[str]:
    """Meta-guardrail, second gate: a protected-path violation makes the
    candidate inadmissible (REJECT via selection.judge's guards).

    This is the fallback until the controller passes domain="self" to
    critic.review(), which is the first gate (GAMING-DETECTED). Both gates
    use the same protected list: the critic's generic patterns PLUS the
    self patterns (see rsi/critic.py PROTECTED_PATH_PATTERNS and
    SELF_PROTECTED_PATH_PATTERNS); keep them in sync. The generic paths
    (verifier, critic, selection, feedback, tests, ledgers) are already
    caught by the critic for any domain, so the guard's extra value is
    the self-only paths (rsi.py, tests/ non-test files, runs/,
    domains/self/*) -- but the guard checks the union so there is one
    obvious protected list, not two half-lists.
    """
    from rsi.critic import (PROTECTED_PATH_PATTERNS,
                            SELF_PROTECTED_PATH_PATTERNS, extract_paths)
    violations = []
    for path in extract_paths(diff or ""):
        for pat, why in PROTECTED_PATH_PATTERNS + SELF_PROTECTED_PATH_PATTERNS:
            if re.search(pat, path):
                violations.append(f"{why}: {path}")
    return violations


class SelfDomain:
    name = "self"
    root = HERE
    critic_patterns = CRITIC_PATTERNS
    components = COMPONENTS

    # ---- task sets ---------------------------------------------------------
    def evolve_ids(self) -> list[str]:
        return [t["task_id"] for t in _load_tasks("evolve.json")]

    def heldout_ids(self) -> list[str]:
        return [t["task_id"] for t in _load_tasks("heldout.json")]

    def load_task(self, task_id: str) -> dict | None:
        for t in _load_tasks("evolve.json") + _load_tasks("heldout.json"):
            if t.get("task_id") == task_id:
                return t
        return None

    # ---- Evaluate ----------------------------------------------------------
    def run(self, root, runs_dir, job, ids, k, log_prefix=""):
        """Run the verifier for each task id in `root` (a worktree path).

        Returns {task_id: Reward}. Rewards are saved under
        runs_dir/jobs/<job>/<task_id>/reward.json.
        """
        root, runs_dir = Path(root), Path(runs_dir)
        out = {}
        for task_id in ids:
            task = self.load_task(task_id)
            if task is None:
                continue
            ev_dir = runs_dir / "jobs" / job / task_id
            reward = run_verifier(root, task, ev_dir / "evidence",
                                  heldout_base=HERE)
            reward.save(ev_dir / "reward.json")
            out[task_id] = reward
        return out

    def score(self, runs_dir, job, ids, k):
        """Score stored rewards: ({task_id: TaskResult}, extra)."""
        runs_dir = Path(runs_dir)
        per, total_pass = {}, 0
        for task_id in ids:
            rj = runs_dir / "jobs" / job / task_id / "reward.json"
            if not rj.exists():
                per[task_id] = sel.TaskResult(rewards=[0.0], missing=1)
                continue
            reward = json.loads(rj.read_text())
            ev = sel.eval_from_reward(task_id, reward, job)
            per[task_id] = sel.TaskResult(
                rewards=[1.0 if ev.f2p_pass and ev.p2p_clean and ev.S >= 1.0 else 0.0],
                weights=[1.0], tokens=[None])
            total_pass += per[task_id].rewards[0]
        n = max(1, len(ids) * k)
        return per, {"total_passes": int(total_pass), "n_trials": n,
                     "pass_rate": total_pass / n}

    # ---- gates -------------------------------------------------------------
    def smoke(self, repo, runs_dir=None, job="smoke", ids=None):
        """Liveness check only: venv python + pytest present, verifier +
        tasks readable. Does NOT run any build or test. Returns (ok, detail).
        """
        repo = Path(repo)
        detail: dict = {}
        detail["venv_python"] = VENV_PYTHON
        detail["venv_python_exists"] = Path(VENV_PYTHON).exists()
        detail["pytest"] = shutil.which("pytest") or "NOT-ON-PATH"
        detail["verifier"] = str(ROOT / "scripts" / "verify.py")
        detail["verifier_exists"] = (ROOT / "scripts" / "verify.py").exists()
        detail["repo_exists"] = (repo / "pyproject.toml").exists()
        detail["rsi_package"] = (repo / "rsi" / "__init__.py").exists()
        detail["tests_dir"] = (repo / "tests").is_dir()
        detail["evolve_tasks"] = len(self.evolve_ids())
        ok = (detail["venv_python_exists"] and detail["verifier_exists"]
              and detail["repo_exists"] and detail["rsi_package"]
              and detail["tests_dir"])
        if not ok:
            detail["note"] = ("smoke FAILED: the repo venv python "
                              f"({VENV_PYTHON}) or the repo layout is "
                              "missing; create the venv and install pytest "
                              "before running self rounds, see "
                              "domains/self/README.md")
        return ok, detail


DOMAIN = SelfDomain()
