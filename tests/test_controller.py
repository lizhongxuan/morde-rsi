import json
import subprocess
from pathlib import Path

from conftest import (BAD_DIFF, FIX_DIFF, GAMING_DIFF, make_task,
                      write_tasks_file)
from rsi.config import RSIConfig
from rsi.controller import Controller
from rsi.fixer import DSHFixer, ManualFixer


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _ctl(tmp_path, repo, tasks, fixer):
    tasks_file = write_tasks_file(tmp_path, tasks)
    runs = tmp_path / "runs"
    return Controller(repo, runs, tasks_file, RSIConfig.load(None), fixer), runs


def _diff_file(tmp_path, text):
    p = tmp_path / "fix.diff"
    p.write_text(text)
    return p


def test_full_round_adopts_good_fix(fixture_repo, heldout_dir, tmp_path):
    task = make_task(heldout_src=heldout_dir)
    ctl, runs = _ctl(tmp_path, fixture_repo, [task],
                     ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "ADOPT", summary
    assert summary["score"] == 1.0
    # Adopted branch exists and contains the fix; main is untouched.
    r = _git(fixture_repo, "rev-parse", "--verify", "rsi/FIX-001")
    assert r.returncode == 0
    assert "return a + b + 1" in (fixture_repo / "calc.py").read_text()  # main tree still buggy
    show = _git(fixture_repo, "show", "rsi/FIX-001:calc.py").stdout
    assert "return a + b\n" in show
    # Frontier + ledger.
    frontier = json.loads((runs / "frontier.json").read_text())
    assert frontier["baseline_commit"] == r.stdout.strip()
    assert frontier["score"] == 1.0
    recs = [json.loads(l) for l in (runs / "history.jsonl").read_text().splitlines()]
    assert recs[-1]["verdict"] == "ADOPT" and recs[-1]["fixer"] == "manual-pilot"
    # No worktrees leak.
    assert len([w for w in _git(fixture_repo, "worktree", "list").stdout.splitlines()
                if w.strip()]) == 1


def test_bad_fix_rejected_by_p2p_gate(fixture_repo, heldout_dir, tmp_path):
    # BAD_DIFF breaks ops.sub: tier A passes (bug fixed? no - it does not
    # touch calc.py, so tier A still fails). Use a diff that fixes calc.py
    # AND breaks ops.py to isolate the P2P gate.
    both = FIX_DIFF + BAD_DIFF
    task = make_task(heldout_src=heldout_dir)
    ctl, runs = _ctl(tmp_path, fixture_repo, [task],
                     ManualFixer(_diff_file(tmp_path, both)))
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "REJECT", summary
    assert "regression" in summary["detail"]
    assert not (runs / "frontier.json").exists()


def test_gaming_diff_detected_before_evaluation(fixture_repo, heldout_dir, tmp_path):
    task = make_task(heldout_src=heldout_dir)
    ctl, runs = _ctl(tmp_path, fixture_repo, [task],
                     ManualFixer(_diff_file(tmp_path, GAMING_DIFF)))
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "GAMING-DETECTED", summary
    recs = [json.loads(l) for l in (runs / "history.jsonl").read_text().splitlines()]
    assert recs[-1]["verdict"] == "GAMING-DETECTED"


def test_baseline_already_passing_is_skipped(fixture_repo, tmp_path):
    import sys
    task = make_task()
    task["task_id"] = "FIX-002"
    task["tiers"]["A"] = {"commands": [[sys.executable, "-m", "pytest", "-q",
                                        "test_other.py"]], "timeout_s": 120}
    task["tiers"]["B"] = {"commands": [[sys.executable, "-m", "pytest", "-q",
                                        "test_other.py"]], "timeout_s": 120}
    task["tiers"]["C"] = {"commands": [[sys.executable, "-m", "pytest", "-q",
                                        "test_other.py"]], "timeout_s": 120,
                          "heldout": {"dest": "."}}
    ctl, _ = _ctl(tmp_path, fixture_repo, [task],
                  ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "SKIPPED", summary


def test_stop_file_ends_run(fixture_repo, heldout_dir, tmp_path):
    task = make_task(heldout_src=heldout_dir)
    ctl, runs = _ctl(tmp_path, fixture_repo, [task],
                     ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    (runs / "STOP").write_text("")
    summaries = ctl.run()
    assert summaries[0]["verdict"] == "STOPPED"


def test_dsh_stub_fixer_is_stuck_not_crash(fixture_repo, heldout_dir, tmp_path):
    task = make_task(heldout_src=heldout_dir)
    ctl, _ = _ctl(tmp_path, fixture_repo, [task], DSHFixer())
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "STUCK", summary
    assert "DSH Task" in summary["detail"] or "stub" in summary["detail"].lower()


def test_run_picks_next_unattempted_task(fixture_repo, heldout_dir, tmp_path):
    t1 = make_task("FIX-001", heldout_src=heldout_dir)
    t2 = make_task("FIX-002", heldout_src=heldout_dir)
    ctl, runs = _ctl(tmp_path, fixture_repo, [t1, t2],
                     ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    summaries = ctl.run()
    # Both tasks get attempted (FIX-002's fix diff also fixes calc.py, so it
    # adopts too); the run ends with DONE when tasks are exhausted.
    assert summaries[0]["task_id"] == "FIX-001"
    assert summaries[1]["task_id"] == "FIX-002"
    assert summaries[-1]["verdict"] in ("DONE", "ADOPT")
    assert ctl.pick_next_task() is None


def _json_suite_script(n_sub: int = 1) -> list:
    """A python one-liner mimicking `go test -json` for tier B, computed
    from the REAL code: TestSub reflects ops.sub (broken by BAD_DIFF),
    TestEnv always fails (environment-dependent, like Morde's DSH tests).
    n_sub=2 adds TestSub2, also broken by BAD_DIFF, for multi-failure
    (clear regression) cases."""
    import sys
    extra = ", 'TestSub2': sub(4, 1) == 3" if n_sub > 1 else ""
    code = (
        "import json, sys\n"
        "from ops import sub\n"
        f"results = {{'TestSub': sub(5, 2) == 3, 'TestEnv': False{extra}}}\n"
        "for name, ok in results.items():\n"
        "    print(json.dumps({'Action': 'run', 'Test': name}))\n"
        "    print(json.dumps({'Action': 'pass' if ok else 'fail', 'Test': name}))\n"
        "sys.exit(0 if all(results.values()) else 1)\n"
    )
    return [sys.executable, "-c", code]


def _p2p_task(heldout_dir, n_sub: int = 1) -> dict:
    task = make_task(heldout_src=heldout_dir)
    task["tiers"]["B"] = {
        "commands": [{"argv": _json_suite_script(n_sub),
                      "format": "go-test-json"}],
        "timeout_s": 120, "mode": "suite-ran"}
    return task


def test_per_test_p2p_baseline_failures_do_not_block(fixture_repo, heldout_dir, tmp_path):
    # TestEnv fails at baseline AND after the fix (environment-dependent,
    # like Morde's DSH handshake tests). It must not block adoption.
    task = _p2p_task(heldout_dir)
    ctl, _ = _ctl(tmp_path, fixture_repo, [task],
                  ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "ADOPT", summary


def test_per_test_p2p_regression_blocks(fixture_repo, heldout_dir, tmp_path):
    # The candidate breaks TWO baseline-passing tests (TestSub and
    # TestSub2): a clear regression, not a single ambiguous point.
    # A single newly-failing test with tiers A and C green now routes to
    # NEEDS_HUMAN instead (CAND-001 lesson; see
    # test_needs_human_single_p2p_failure).
    task = _p2p_task(heldout_dir, n_sub=2)
    ctl, _ = _ctl(tmp_path, fixture_repo, [task],
                  ManualFixer(_diff_file(tmp_path, FIX_DIFF + BAD_DIFF)))
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "REJECT", summary
    assert "regression" in summary["detail"]


# ------------------------------------------------------- feedback: NEEDS_HUMAN --
def test_needs_human_single_p2p_failure(fixture_repo, heldout_dir, tmp_path):
    """CAND-001 shape, per-test map: fix is right, one baseline-passing
    test now fails, tiers A and C green -> NEEDS_HUMAN, not REJECT."""
    from rsi.feedback import FeedbackRecord
    task = _p2p_task(heldout_dir)
    ctl, runs = _ctl(tmp_path, fixture_repo, [task],
                     ManualFixer(_diff_file(tmp_path, FIX_DIFF + BAD_DIFF)))
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "NEEDS_HUMAN", summary
    assert summary["failed_tests"] == ["TestSub"]
    assert summary["decision"]["decision"] == "NEEDS_HUMAN"
    # Branch kept for human review; frontier untouched; card written.
    assert _git(fixture_repo, "rev-parse", "--verify",
                "rsi/FIX-001").returncode == 0
    assert not (runs / "frontier.json").exists()
    card = json.loads((runs / "approval-FIX-001-c1.json").read_text())
    assert card["failed_tests"] == ["TestSub"]
    assert card["actions"] and len(card["actions"]) == 3
    assert "diff --git" not in json.dumps(card)
    recs = [json.loads(l) for l in (runs / "history.jsonl").read_text().splitlines()]
    assert recs[-1]["verdict"] == "NEEDS_HUMAN"
    # No resolution yet: the task is not retried.
    assert ctl.pick_next_task() is None
    assert ctl.status()["needs_human"] == ["FIX-001"]


def test_needs_human_resolve_requeues_and_adopts(fixture_repo, heldout_dir,
                                                 tmp_path):
    """Human resolves (updates the test expectation, outside RSI), the
    task becomes retryable, and the good fix then adopts."""
    from rsi.feedback import FeedbackRecord
    task = _p2p_task(heldout_dir)
    both = _diff_file(tmp_path, FIX_DIFF + BAD_DIFF)
    ctl, runs = _ctl(tmp_path, fixture_repo, [task], ManualFixer(both))
    assert ctl.run_task(task, 0)["verdict"] == "NEEDS_HUMAN"
    assert ctl.pick_next_task() is None
    # Resolution for round 0 requeues; a resolution for an older round
    # would not (see test_feedback.test_has_resolution_is_round_scoped).
    ctl.feedback_log.append(FeedbackRecord(
        candidate_id="FIX-001-c1", task_id="FIX-001",
        kind="human_resolution", source="human", label=None, round_no=0,
        note="update-test-expectation: TestSub now expects the new behaviour"))
    assert ctl.pick_next_task()["task_id"] == "FIX-001"
    assert ctl.status()["needs_human"] == []
    ctl.fixer = ManualFixer(_diff_file(tmp_path, FIX_DIFF))
    summary = ctl.run_task(task, 1)
    assert summary["verdict"] == "ADOPT", summary
    assert (runs / "frontier.json").exists()


def test_delayed_recheck_after_adopt(fixture_repo, heldout_dir, tmp_path):
    task = make_task(heldout_src=heldout_dir)
    ctl, runs = _ctl(tmp_path, fixture_repo, [task],
                     ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    assert ctl.run_task(task, 0)["verdict"] == "ADOPT"
    out = ctl.delayed_recheck(task, "FIX-001-c1")
    assert out["passed"] is True and out["label"] == "correct"
    tele = [json.loads(l)
            for l in (runs / "telemetry.jsonl").read_text().splitlines()]
    assert len(tele) == 1 and tele[0]["kind"] == "delayed_recheck"
    assert tele[0]["candidate_id"] == "FIX-001-c1"


def _vitest_fail_task(heldout_dir, behavior_tests):
    """Tier B prints a vitest-style FAIL line and exits 1 (no per-test map)."""
    import sys
    task = make_task(heldout_src=heldout_dir)
    code = ("import sys\n"
            "print('FAIL src/routes/Projects.test.tsx')\n"
            "sys.exit(1)\n")
    task["tiers"]["B"] = {"commands": [[sys.executable, "-c", code]],
                          "timeout_s": 120}
    if behavior_tests is not None:
        task["behavior_tests"] = behavior_tests
    return task


def test_needs_human_via_vitest_fail_log(fixture_repo, heldout_dir, tmp_path):
    """No per-test map: one FAIL line naming a declared behavior test,
    tiers A and C green -> NEEDS_HUMAN (the CAND-001 / Portal shape)."""
    task = _vitest_fail_task(heldout_dir, ["Projects.test.tsx"])
    ctl, runs = _ctl(tmp_path, fixture_repo, [task],
                     ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "NEEDS_HUMAN", summary
    assert summary["failed_tests"] == ["src/routes/Projects.test.tsx"]
    card = json.loads((runs / "approval-FIX-001-c1.json").read_text())
    assert "Projects.test.tsx" in card["failed_tests"][0]


def test_vitest_fail_without_declared_tests_stays_reject(fixture_repo,
                                                        heldout_dir, tmp_path):
    """Same failure, but the task declares no behavior tests, so the
    single point cannot be attributed -> clear regression -> REJECT."""
    task = _vitest_fail_task(heldout_dir, None)
    ctl, runs = _ctl(tmp_path, fixture_repo, [task],
                     ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "REJECT", summary
    assert not (runs / "approval-FIX-001-c1.json").exists()


# ------------------------------------------------- stall redirect (components) --
def _stalled_history(ctl, improving: bool = False):
    """Four flat REJECT rounds (scores 0.5) so the run is stalled
    (w=3, delta=0.02, trajectory [0,0,0,0] + incumbent 0.0). logic-small
    fails 3x in a row -> stuck; portal-ui fails once -> not stuck.
    With improving=True an ADOPT at round 2 lifts the trajectory, so
    the run is NOT stalled even though logic-small stays stuck."""
    h = ctl.history
    for rnd, (tid, comp, verdict, score) in enumerate([
            ("S1", "logic-small", "REJECT", 0.5),
            ("S2", "logic-small", "REJECT", 0.5),
            ("S3", "portal-ui", "ADOPT" if improving else "REJECT",
             0.7 if improving else 0.5),
            ("S4", "logic-small", "REJECT", 0.5)]):
        h.append_candidate(rnd, tid, "c1", "A", comp, None, "d", {},
                           verdict, "aaa", score, None, 1.0)


def _stall_tasks(heldout_dir, components):
    tasks = []
    for i, comp in enumerate(components):
        t = make_task(f"T{i + 1}", heldout_src=heldout_dir)
        t["component"] = comp
        tasks.append(t)
    return tasks


def test_pick_next_task_skips_stuck_component_when_stalled(
        fixture_repo, heldout_dir, tmp_path):
    tasks = _stall_tasks(heldout_dir, ["logic-small", "portal-ui"])
    ctl, _ = _ctl(tmp_path, fixture_repo, tasks,
                  ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    _stalled_history(ctl)
    assert ctl._stall_state()["stall"] == 1
    nxt = ctl.pick_next_task()
    assert nxt is not None and nxt["task_id"] == "T2"
    assert nxt["component"] == "portal-ui"


def test_pick_next_task_none_and_run_stalled_when_all_stuck(
        fixture_repo, heldout_dir, tmp_path):
    tasks = _stall_tasks(heldout_dir, ["logic-small"])
    ctl, _ = _ctl(tmp_path, fixture_repo, tasks,
                  ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    _stalled_history(ctl)
    assert ctl.pick_next_task() is None
    summaries = ctl.run()
    assert summaries[0]["verdict"] == "STALLED"
    assert "stuck" in summaries[0]["detail"]


def test_pick_next_task_no_skip_when_not_stalled(
        fixture_repo, heldout_dir, tmp_path):
    tasks = _stall_tasks(heldout_dir, ["logic-small", "portal-ui"])
    ctl, _ = _ctl(tmp_path, fixture_repo, tasks,
                  ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    _stalled_history(ctl, improving=True)
    assert ctl._stall_state()["stall"] == 0
    # logic-small is still stuck, but without a stall there is no skip.
    assert ctl.history.stuck_components() == {"logic-small"}
    assert ctl.pick_next_task()["task_id"] == "T1"


class _CapturingFixer(ManualFixer):
    """ManualFixer that keeps the task dict it was handed (the brief)."""

    def fix(self, task, worktree, budget):
        self.seen = dict(task)
        return super().fix(task, worktree, budget)


def test_stalled_round_carries_exploration_to_fixer_and_ledger(
        fixture_repo, heldout_dir, tmp_path):
    task = make_task(heldout_src=heldout_dir)
    fixer = _CapturingFixer(_diff_file(tmp_path, FIX_DIFF))
    ctl, runs = _ctl(tmp_path, fixture_repo, [task], fixer)
    _stalled_history(ctl)
    summary = ctl.run_task(task, 4)
    assert summary["verdict"] == "ADOPT", summary
    assert summary["stall"] == 1
    assert "停滞" in summary["exploration"]
    assert summary["prune_set"][0]["component"] == "logic-small"
    # The brief the Fixer got carries the redirection (fixer.py untouched:
    # the dict is the carrier, render_brief has no exploration slot).
    assert fixer.seen["exploration"] == summary["exploration"]
    assert fixer.seen["stall_flag"] == 1
    assert fixer.seen["prune_set"] == ["logic-small"]
    # The round's ledger record carries the stall note + the label the
    # diff evidences (calc.py -> logic-small).
    recs = [json.loads(l)
            for l in (runs / "history.jsonl").read_text().splitlines()]
    assert "stall=1" in recs[-1]["detail"]
    assert recs[-1]["components"] == ["logic-small"]
    assert recs[-1]["component"] == "logic-small"
    # status() after the round: the ADOPT moved the incumbent to 1.0,
    # so the stall is broken (stall=0) and the prune clears (logic-small
    # now has an adoption in the window). That is the mechanism working:
    # a stalled run that finally adopts stops being stalled.
    status = ctl.status()
    assert status["stall"] == 0
    assert status["component_summary"]["logic-small"] == {
        "attempts": 4, "adopt": 1, "reject": 3, "decided": 4,
        "win_rate": 0.25}
    assert status["prune_set"] == []


# ---- integration: delta resolution, critic domain wiring, pytest FAIL ----
def test_resolve_delta_fallback_when_unmeasured(tmp_path, fixture_repo):
    tasks_file = write_tasks_file(tmp_path, [])
    cfg = RSIConfig.load(None)
    cfg.delta = None
    ctl = Controller(fixture_repo, tmp_path / "runs", tasks_file, cfg, None)
    assert ctl._resolve_delta(allow_calibrate=False) == (
        0.02, "fallback-0.02-unmeasured")
    # No baseline evaluations on disk: auto-calibration has nothing to
    # read, so it must fall back too -- never invent a delta.
    assert ctl._resolve_delta(allow_calibrate=True) == (
        0.02, "fallback-0.02-unmeasured")


def test_resolve_delta_reads_calibration_json(tmp_path, fixture_repo):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "calibration.json").write_text(json.dumps({"delta": 0.05}))
    tasks_file = write_tasks_file(tmp_path, [])
    cfg = RSIConfig.load(None)
    cfg.delta = None
    ctl = Controller(fixture_repo, runs, tasks_file, cfg, None)
    assert ctl._resolve_delta() == (0.05, "calibration.json")


def test_delta_none_round_completes_without_crash(
        fixture_repo, heldout_dir, tmp_path):
    # delta=null with no calibration file used to crash judge() with a
    # TypeError; the round must now complete (fallback/auto-calibrated).
    task = make_task(heldout_src=heldout_dir)
    tasks_file = write_tasks_file(tmp_path, [task])
    runs = tmp_path / "runs"
    cfg = RSIConfig.load(None)
    cfg.delta = None
    ctl = Controller(fixture_repo, runs, tasks_file, cfg,
                     ManualFixer(_diff_file(tmp_path, FIX_DIFF)))
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "ADOPT", summary
    assert isinstance(summary["delta"], float)
    assert summary["delta_source"] in ("auto-calibrated",
                                       "fallback-0.02-unmeasured")
    # Baseline measurement was persisted for later calibration/analysis.
    assert (runs / "reward-FIX-001-base.json").exists()
    assert (runs / "reward-FIX-001-c1.json").exists()
    assert list(runs.glob("baseline-eval-FIX-001-*.json"))


def test_failed_files_from_evidence_parses_pytest(tmp_path):
    from rsi.controller import _failed_files_from_evidence
    ev = tmp_path / "evidence"
    ev.mkdir()
    (ev / "B-0.log").write_text(
        "FAILED tests/test_packaging_repro.py::test_x - assert False\n"
        "FAILED tests/test_other.py::test_y\n")
    assert _failed_files_from_evidence(ev) == [
        "tests/test_other.py", "tests/test_packaging_repro.py"]
    # vitest style still works alongside.
    (ev / "B-1.log").write_text(
        "FAIL  web/portal/src/routes/Projects.test.tsx\n")
    assert "web/portal/src/routes/Projects.test.tsx" in (
        _failed_files_from_evidence(ev))


def test_self_domain_critic_blocks_verifier_edit(tmp_path):
    # The self loop grades its own repo: a diff touching the verifier
    # must be GAMING-DETECTED by the critic (first gate), never evaluated.
    # The fixture repo contains rsi/verifier.py so the manual diff applies.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "rsi-test@example.invalid")
    _git(repo, "config", "user.name", "rsi-test")
    (repo / "rsi").mkdir()
    (repo / "rsi" / "verifier.py").write_text("PASS = False\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fixture")
    task = {
        "task_id": "SELF-TEST-1",
        "title": "x",
        "description": "x",
        "component": "config-default",
        "hypothesis": "x",
        "tiers": {
            "A": {"commands": [["false"]], "timeout_s": 30},
            "B": {"commands": [["true"]], "timeout_s": 30},
            "C": {"commands": [["true"]], "timeout_s": 30},
        },
    }
    diff = ("diff --git a/rsi/verifier.py b/rsi/verifier.py\n"
            "--- a/rsi/verifier.py\n+++ b/rsi/verifier.py\n"
            "@@ -1 +1 @@\n-PASS = False\n+PASS = True\n")
    tasks_file = write_tasks_file(tmp_path, [task])
    runs = tmp_path / "runs"
    ctl = Controller(repo, runs, tasks_file, RSIConfig.load(None),
                     ManualFixer(_diff_file(tmp_path, diff)), domain="self")
    summary = ctl.run_task(task, 0)
    assert summary["verdict"] == "GAMING-DETECTED", summary
    assert "verifier module touched" in summary["detail"]


def test_morde_domain_same_diff_is_not_self_gaming(tmp_path):
    # The same verifier-editing diff under the morde domain is still
    # caught by the generic list (verifier is protected for every domain),
    # but a self-only path (e.g. rsi.py) is NOT gaming for morde: the
    # morde Fixer works in the Morde repo and can never reach it.
    from rsi import critic as critic_mod
    diff_rsi_py = ("diff --git a/rsi.py b/rsi.py\n"
                   "--- a/rsi.py\n+++ b/rsi.py\n"
                   "@@ -1 +1 @@\n-x = 1\n+x = 2\n")
    assert critic_mod.review(diff_rsi_py, domain="self")["gaming"] is True
    assert critic_mod.review(diff_rsi_py, domain="morde")["verdict"] == "accept"
    assert critic_mod.review(diff_rsi_py)["verdict"] == "accept"
