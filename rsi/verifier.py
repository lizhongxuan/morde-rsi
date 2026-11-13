"""The Judge: a deterministic verification script, never an agent.

Input: a worktree path + a task definition (JSON). Tiers, in order:

  A  build + vet + targeted unit tests (the task's own commands).
     A repro test file that does not exist in the repo yet is injected
     from outside the worktree before A runs and removed right after,
     so the Fixer never sees it and tier B measures only pre-existing
     tests. If A fails, B and C are skipped (marked "skipped", passed=False).
  B  regression (the task's own commands, e.g. go test ./... / vitest run).
  C  held-out tests: the verifier copies held-out files from a directory
     OUTSIDE the worktree into it, runs them, then deletes them. The Fixer
     never sees held-out material; the injection paths are owned by the
     verifier, not the Fixer.

Output: reward.json — per-tier pass/fail, per-check evidence paths, durations,
score (fraction of executed checks passed). Exit status of scripts/verify.py
is 0 iff every executed tier passed.

A check is one command. A missing command (binary not found) fails the check
with the stderr recorded; it is never silently skipped, so a candidate cannot
look better by destroying its own verification.

k-trial (RRSI evaluate.py aggregation): a task may name flaky checks in
`flaky_checks` (check names as in reward.json, e.g. "B-0" = tier B command 0)
and set `k` (or the caller passes k). Only the named checks run k times;
every other check runs once, exactly as before. Each trial is recorded in
the check's `trials` list (passed / returncode / missing / output_path) and
counts as one reward in selection.eval_from_reward, so a check passing 1 of
2 trials scores 0.5. A trial that times out or whose binary is missing has
returncode None and missing=True: it contributes 0 with the denominator
kept, never an absent slot. The merged `passed` stays strict (all trials
must pass) so the F2P/P2P hard gates never loosen because a check is flaky.
For go-test-json checks the per-test map merges with AND across trials.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    command: list[str]
    passed: bool
    returncode: int | None
    duration_s: float
    output_path: str | None = None
    tests: dict = field(default_factory=dict)  # test name -> passed (go-test-json)
    # One entry per trial that ran this check (k=1: exactly one entry):
    # {"passed": bool, "returncode": int | None, "missing": bool,
    #  "output_path": str | None}. missing=True iff the trial never ran
    # (timeout or binary not found, i.e. returncode is None).
    trials: list = field(default_factory=list)


@dataclass
class TierResult:
    name: str
    passed: bool
    status: str = "run"           # run | skipped
    checks: list[CheckResult] = field(default_factory=list)
    duration_s: float = 0.0


@dataclass
class Reward:
    task_id: str
    worktree: str
    tiers: dict                   # tier name -> TierResult (as dict)
    passed: bool
    score: float                  # passed checks / executed checks
    duration_s: float
    evidence_dir: str

    def to_json(self) -> dict:
        return {"task_id": self.task_id, "worktree": self.worktree,
                "tiers": self.tiers, "passed": self.passed,
                "score": self.score, "duration_s": round(self.duration_s, 1),
                "evidence_dir": self.evidence_dir}

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=1,
                                         ensure_ascii=False))


def _parse_go_test_json(stdout: str) -> dict:
    """Per-test results from `go test -json` output.

    Keeps top-level tests and subtests keyed by their full name; skips are
    omitted (neither pass nor fail). Package-level events have no Test field
    and are ignored here.
    """
    out: dict[str, bool] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        test = ev.get("Test")
        action = ev.get("Action")
        if test and action in ("pass", "fail"):
            out[test] = action == "pass"
    return out


def _run_one(worktree: Path, name: str, command, timeout_s: int,
             evidence_dir: Path, mode: str = "all-pass") -> CheckResult:
    """Run one check. `command` is an argv list, or a dict:
    {"cwd": "server", "argv": [...], "format": "go-test-json"}.

    `mode` decides what "passed" means for a go-test-json command:
    - "all-pass" (tiers A and C): rc == 0 and every parsed test passes.
      A failing repro test fails the check.
    - "suite-ran" (tier B): the suite ran to completion (rc 0 or 1) and at
      least one test result was parsed. Individual failures are recorded in
      `tests` and judged per-test against the baseline (P2P), not here.
      Morde's baseline already has environment-dependent failures
      (DSH handshake, browser timeouts), so a whole-package pass/fail
      cannot be the gate.
    Plain (non-JSON) commands: passed = rc == 0.
    """
    if isinstance(command, dict):
        cwd = worktree / command.get("cwd", ".")
        argv = list(command["argv"])
        fmt = command.get("format")
    else:
        cwd, argv = worktree, list(command)
        fmt = None
    t0 = time.time()
    out_path = evidence_dir / f"{name}.log"
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout_s)
        rc = r.returncode
        text = f"$ {' '.join(argv)} (cwd={cwd})\nrc={rc}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    except FileNotFoundError as e:
        rc, r = None, None
        text = f"$ {' '.join(argv)} (cwd={cwd})\nERROR: binary not found: {e}"
    except subprocess.TimeoutExpired:
        rc, r = None, None
        text = f"$ {' '.join(argv)} (cwd={cwd})\nERROR: timed out after {timeout_s}s"
    out_path.write_text(text)
    tests: dict[str, bool] = {}
    if fmt == "go-test-json" and r is not None:
        tests = _parse_go_test_json(r.stdout)
        if not tests:
            ok = rc == 0
        elif mode == "suite-ran":
            ok = rc in (0, 1)
        else:
            ok = rc == 0 and all(tests.values())
    else:
        ok = rc == 0
    result = CheckResult(name=name, command=argv, passed=ok, returncode=rc,
                         duration_s=round(time.time() - t0, 1),
                         output_path=str(out_path), tests=tests)
    # A single run is a single trial; _run_trials merges these per trial.
    result.trials = [{"passed": ok, "returncode": rc, "missing": rc is None,
                      "output_path": str(out_path)}]
    return result


def _run_trials(worktree: Path, name: str, command, timeout_s: int,
                evidence_dir: Path, mode: str = "all-pass",
                k: int = 1) -> CheckResult:
    """Run one check k times and merge the trials into one CheckResult.

    k=1 is exactly the old single run. For k>1 each trial gets its own
    evidence log (<name>.t<j>.log); `output_path` points at trial 0's log.
    The merged `passed` is strict (all trials must pass) and the per-test
    map merges with AND, so k-trial never loosens a gate -- it only refines
    the score, which selection.eval_from_reward reads from `trials`.
    """
    results = []
    for j in range(k):
        log_name = f"{name}.t{j}" if k > 1 else name
        r = _run_one(worktree, log_name, command, timeout_s,
                     evidence_dir, mode=mode)
        r.name = name
        results.append(r)
    tests: dict[str, bool] = {}
    for r in results:
        for t, ok in r.tests.items():
            tests[t] = tests.get(t, True) and ok
    return CheckResult(name=name, command=results[0].command,
                       passed=all(r.passed for r in results),
                       returncode=results[-1].returncode,
                       duration_s=round(sum(r.duration_s for r in results), 1),
                       output_path=results[0].output_path,
                       tests=tests,
                       trials=[r.trials[0] for r in results])


def _run_tier(worktree: Path, tier_name: str, tier_cfg: dict,
              evidence_dir: Path, k: int = 1,
              flaky_checks: frozenset | set = frozenset()) -> TierResult:
    t0 = time.time()
    timeout_s = int(tier_cfg.get("timeout_s", 600))
    mode = tier_cfg.get("mode", "all-pass")
    checks = []
    for i, cmd in enumerate(tier_cfg.get("commands", [])):
        # Pass dict commands through unchanged; copy plain argv lists.
        cmd = list(cmd) if isinstance(cmd, list) else cmd
        name = f"{tier_name}-{i}"
        # Only checks named in the task's flaky_checks run k times; the
        # rest run once even when k > 1.
        n = k if name in flaky_checks else 1
        checks.append(_run_trials(worktree, name, cmd, timeout_s,
                                  evidence_dir, mode=mode, k=n))
    passed = all(c.passed for c in checks) and bool(checks)
    return TierResult(name=tier_name, passed=passed, status="run",
                      checks=checks, duration_s=round(time.time() - t0, 1))


def _inject_files(worktree: Path, spec: dict,
                  heldout_base: Path | str | None) -> list[Path]:
    """Copy verifier-owned test files into a worktree.

    `spec` is {"src": <dir outside the worktree>, "dest": <dir inside it>};
    a relative src is resolved against `heldout_base`. Returns the injected
    paths (delete them after the tier runs). Used for the tier A repro test
    and the tier C held-out tests: both are verifier material the Fixer
    never sees.
    """
    injected: list[Path] = []
    src = Path(spec["src"]) if spec.get("src") else None
    if src is not None and not src.is_absolute() and heldout_base:
        src = Path(heldout_base) / src
    dest = Path(spec["dest"]) if spec.get("dest") else None
    if src and dest and src.exists():
        target = worktree / dest
        target.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if f.is_file():
                shutil.copy2(f, target / f.name)
                injected.append(target / f.name)
    return injected


def _remove_files(paths: list[Path]) -> None:
    for f in paths:
        f.unlink(missing_ok=True)


def run_verifier(worktree: Path | str, task: dict,
                 evidence_dir: Path | str, heldout_base: Path | str | None = None,
                 fail_fast: bool = True, k: int | None = None) -> Reward:
    """Run tiers A/B/C for one task in one worktree. Returns a Reward.

    A relative tier-C held-out `src` is resolved against `heldout_base`
    (the domain root when called from the controller, the task file's
    directory when called from scripts/verify.py); with no base it is
    resolved against the current working directory.

    `fail_fast` (default True): if tier A fails, B and C are skipped.
    The BASELINE run uses fail_fast=False: the baseline must capture the
    full per-test map (tier B's P2P set, tier C's held-out baseline) even
    though the bug reproduces in tier A.

    `k` (default 1): trials for the checks the task names in
    `flaky_checks` (check names as in reward.json, e.g. "B-0"). An explicit
    `k` argument wins; otherwise the task JSON's own "k" is used. Only the
    named checks run k times -- undeclared checks always run once, so the
    pilot default k=1 changes nothing. The merged check `passed` stays
    strict (all trials must pass); the per-trial scores live in each
    check's `trials` list and are aggregated by selection.eval_from_reward.
    """
    worktree, evidence_dir = Path(worktree), Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    tiers: dict[str, TierResult] = {}
    tier_cfgs = task.get("tiers", {})
    k_eff = max(1, int(k if k is not None else task.get("k", 1)))
    flaky = set(task.get("flaky_checks") or [])

    # Tier A: build + vet + targeted unit tests. Fail-fast (unless the
    # caller asked for the full baseline map). The repro test (if any) is
    # injected before tier A and removed right after, so tier B measures
    # only pre-existing tests and the Fixer never sees the repro file.
    a_cfg = tier_cfgs.get("A", {})
    a_injected = _inject_files(worktree, a_cfg.get("repro") or {},
                               heldout_base)
    try:
        tiers["A"] = _run_tier(worktree, "A", a_cfg, evidence_dir,
                               k=k_eff, flaky_checks=flaky)
    finally:
        _remove_files(a_injected)
    if fail_fast and not tiers["A"].passed:
        for name in ("B", "C"):
            tiers[name] = TierResult(name=name, passed=False, status="skipped")
    else:
        tiers["B"] = _run_tier(worktree, "B", tier_cfgs.get("B", {}),
                               evidence_dir, k=k_eff, flaky_checks=flaky)
        # Tier C: inject held-out tests from OUTSIDE the worktree, run, delete.
        c_cfg = tier_cfgs.get("C", {})
        c_injected = _inject_files(worktree, c_cfg.get("heldout") or {},
                                   heldout_base)
        try:
            tiers["C"] = _run_tier(worktree, "C", c_cfg, evidence_dir,
                                   k=k_eff, flaky_checks=flaky)
        finally:
            _remove_files(c_injected)

    executed = [c for t in tiers.values() if t.status == "run" for c in t.checks]
    n_pass = sum(1 for c in executed if c.passed)
    score = (n_pass / len(executed)) if executed else 0.0
    passed = all(t.passed for t in tiers.values())
    return Reward(task_id=task.get("task_id", "?"), worktree=str(worktree),
                  tiers={k: asdict(v) for k, v in tiers.items()},
                  passed=passed, score=round(score, 4),
                  duration_s=round(time.time() - t0, 1),
                  evidence_dir=str(evidence_dir))
