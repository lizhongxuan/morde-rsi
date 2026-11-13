"""Morde domain adapter.

RRSI-shaped Domain (cf. refs/rrsi/domains/coding/adapter.py): the same
interface (evolve_ids / heldout_ids / run / score / smoke / critic_patterns /
guards), but the evaluation backend is a local git worktree + the deterministic
verifier script (rsi/verifier.py) instead of harbor + Docker, and the score
comes from Morde's own go test / vitest / Playwright checks instead of a
benchmark suite.
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

# Component vocabulary for the proposer (see SKILL.md).
COMPONENTS = ["ui-text", "ui-style", "null-check", "error-message",
              "config-default", "logic-small"]

# Domain critic patterns, applied to changed file paths in the diff
# (in addition to rsi/critic.py's generic protected paths).
CRITIC_PATTERNS = [
    (r"(^|/)web/portal/e2e/", "portal e2e directory touched"),
    (r"(^|/)tasks/heldout", "held-out task material touched"),
    (r"schema\.d\.ts$", "generated API schema touched"),
    (r"(^|/)src/mocks/", "test mocks touched"),
]

# Pilot guards: paths the pilot must never touch, even if the critic allows
# them. A violation makes the candidate inadmissible (not GAMING-DETECTED:
# this is scope, not cheating).
GUARD_PATTERNS = [
    (r"server/internal/app/agent_tools\.go$", "approval-card code (pilot scope)"),
    (r"server/internal/worker/cloud_dsh_coordinator\.go$", "DSH coordinator (pilot scope)"),
    (r"server/migrations/", "database migration (pilot scope)"),
    (r"runtime/morde-cloud-runtime/package\.json$", "DSH version pin (pilot scope)"),
    (r"(?i)(credential|secret|private[_-]?key)", "credential/secret material (pilot scope)"),
]


def _load_tasks(fname: str) -> list[dict]:
    p = HERE / "tasks" / fname
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return data.get("tasks", []) if isinstance(data, dict) else data


def guards_for_diff(diff: str) -> list[str]:
    """Pilot scope guards, checked against the changed file paths."""
    from rsi.critic import extract_paths
    violations = []
    for path in extract_paths(diff or ""):
        for pat, why in GUARD_PATTERNS:
            if re.search(pat, path):
                violations.append(f"{why}: {path}")
    return violations


class MordeDomain:
    name = "morde"
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
        """Liveness check only: tools present, verifier + tasks readable.

        Does NOT run any build or test. Returns (ok, detail).
        """
        repo = Path(repo)
        detail: dict = {}
        for tool in ("go", "node", "npx"):
            detail[tool] = shutil.which(tool) or "MISSING"
        detail["verifier"] = str(ROOT / "scripts" / "verify.py")
        detail["verifier_exists"] = (ROOT / "scripts" / "verify.py").exists()
        detail["repo_exists"] = (repo / "server" / "go.mod").exists()
        detail["portal_package_json"] = (repo / "web" / "portal" / "package.json").exists()
        detail["evolve_tasks"] = len(self.evolve_ids())
        ok = (detail["go"] != "MISSING" and detail["node"] != "MISSING"
              and detail["verifier_exists"] and detail["repo_exists"]
              and detail["portal_package_json"])
        if not ok:
            detail["note"] = ("smoke FAILED: install the missing tools "
                              "(go / node) and confirm the repo path before "
                              "the pilot; see domains/morde/README.md")
        return ok, detail


DOMAIN = MordeDomain()
