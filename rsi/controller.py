"""The Controller: the outer loop's state machine.

    DISCOVER -> BASELINE -> FIX -> VERIFY -> ADOPT / REJECT -> NEXT

* DISCOVER: pick the next unattempted task from domains/morde/tasks/evolve.json
  (the discoverer is a stub in the pilot: tasks are listed by a human).
* BASELINE: run the verifier on a clean Judge worktree at the baseline commit.
  If the bug does not reproduce (baseline already passes), the task is SKIPPED.
* FIX: the Fixer works in its own worktree (max_submissions per task).
* VERIFY: critic first (a gaming hit ends the round as GAMING-DETECTED,
  before any evaluation is spent); then a clean Judge worktree is built from
  the candidate diff and the verifier runs tiers A/B/C.
* ADOPT / REJECT: selection.judge() (noise floor + cost rule + F2P/P2P hard
  gate). ADOPT commits the candidate to branch rsi/<task-id> and records the
  new baseline in frontier.json. NEVER merges into main/master.
* NEXT: worktrees are cleaned up; the round is written to the JSONL ledger.

Runs are resumable: runs/<name>/frontier.json + history.jsonl hold all state.
A STOP file in the runs dir ends the run gracefully after the current round.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import critic as critic_mod
from . import feedback as fb
from . import selection as sel
from . import worktree as wt
from .calibrate import (auto_calibrate, read_calibration,
                        record_baseline_eval)
from .components import normalize as normalize_component
from .config import RSIConfig
from .fixer import FixResult
from .history import History, exploration, stall_flag
from .verifier import Reward, run_verifier


def _failed_files_from_evidence(evidence_dir: Path | str,
                                tier: str = "B") -> list[str]:
    """Failing test files named in a tier's evidence logs.

    The per-test P2P map only exists for go-test-json tiers. Plain-command
    tiers name their failures in the log instead: vitest/jest print lines
    like ``FAIL  web/portal/src/routes/Projects.test.tsx`` and pytest -q
    prints ``FAILED tests/test_x.py::test_y - ...``. This parser reads both
    so the NEEDS_HUMAN router can still attribute a lone failure. Returns
    [] when nothing parseable is found (caller must then treat the failure
    as unattributable, i.e. a clear regression, not an ambiguous one).
    """
    import re
    out: set[str] = set()
    evidence_dir = Path(evidence_dir)
    if not evidence_dir.is_dir():
        return []
    for log in evidence_dir.glob(f"{tier}-*.log"):
        for line in log.read_text(errors="replace").splitlines():
            m = re.match(r"\s*FAIL\s+(\S+)", line)
            if m:
                out.add(m.group(1))
                continue
            m = re.match(r"\s*FAILED\s+(\S+?)(?:::|\s+-|\s*$)", line)
            if m:
                out.add(m.group(1))
    return sorted(out)


class Controller:
    def __init__(self, repo: Path | str, runs_dir: Path | str,
                 tasks_file: Path | str, config: RSIConfig,
                 fixer=None, history: History | None = None,
                 skill_md: str = "", guard_fn=None,
                 feedback_log: fb.FeedbackLog | None = None,
                 telemetry_log: fb.FeedbackLog | None = None,
                 domain: str | None = None, domain_patterns=None,
                 worktree_excludes: list[str] | None = None,
                 domain_components: list[str] | None = None):
        self.repo = Path(repo)
        self.runs_dir = Path(runs_dir)
        self.tasks_file = Path(tasks_file)
        self.cfg = config
        self.fixer = fixer
        self.skill_md = skill_md
        self.guard_fn = guard_fn  # callable diff -> list[str] violations
        # Domain identity for the critic (domain="self" turns on the
        # meta-guardrail blacklist; any other value leaves it off) and
        # the domain's own critic patterns (RRSI: domain patterns apply
        # on top of the generic protected list). worktree_excludes are
        # worktree-relative paths removed from the Fixer worktree before
        # it starts (self domain: the task material, so the Fixer cannot
        # read its own exam) and excluded from its diff.
        self.domain = domain
        self.domain_patterns = list(domain_patterns or [])
        self.worktree_excludes = list(worktree_excludes or [])
        # Component vocabulary for stall/exploration text: the domain's
        # own COMPONENTS when it declares one, else Morde's K. Without
        # this the self loop would be told to explore Morde-only
        # components (e.g. portal-ui) that do not exist in its repo.
        self.domain_components = (list(domain_components)
                                  if domain_components else None)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.history = history or History(self.runs_dir / "history.jsonl")
        # Feedback logs (rsi/feedback.py). human_feedback.jsonl is what a
        # human tapped/typed (layer 2/3); telemetry.jsonl is derived
        # behaviour (layer 1). Both are separate from history.jsonl and
        # reward.json, and neither is ever passed to the Fixer raw.
        self.feedback_log = (feedback_log or
                             fb.FeedbackLog(self.runs_dir / "human_feedback.jsonl"))
        self.telemetry_log = (telemetry_log or
                              fb.FeedbackLog(self.runs_dir / "telemetry.jsonl"))
        self.frontier_path = self.runs_dir / "frontier.json"
        self.stop_path = self.runs_dir / "STOP"

    # ------------------------------------------------------------ state ---
    def frontier(self) -> dict:
        if self.frontier_path.exists():
            return json.loads(self.frontier_path.read_text())
        return self.history.frontier()

    def _save_frontier(self, fr: dict) -> None:
        self.frontier_path.write_text(json.dumps(fr, indent=1,
                                                 ensure_ascii=False))

    def baseline_commit(self) -> str:
        fr = self.frontier()
        return fr.get("baseline_commit") or wt.head_commit(self.repo)

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def tasks(self) -> list[dict]:
        if not self.tasks_file.exists():
            return []
        data = json.loads(self.tasks_file.read_text())
        return data.get("tasks", []) if isinstance(data, dict) else data

    def _resolve_delta(self, allow_calibrate: bool = False
                       ) -> tuple[float, str]:
        """The noise band for this round, and where it came from.

        Priority: explicit config delta > runs/<domain>/calibration.json
        (written by `rsi.py calibrate`) > auto-calibration from recorded
        baseline evaluations (only when allow_calibrate, i.e. inside a
        round after a baseline has just been recorded) > 0.02 fallback.
        The fallback is the old pilot placeholder, used only so a round
        never crashes on an unmeasured delta; its source is reported in
        the round summary and status so it is never mistaken for a
        measurement.
        """
        if self.cfg.delta is not None:
            return float(self.cfg.delta), "config"
        cal = read_calibration(self.runs_dir)
        if cal is not None:
            return cal, "calibration.json"
        if allow_calibrate:
            try:
                c, _ = auto_calibrate(self.runs_dir, z=self.cfg.delta_z)
                return float(c["delta"]), "auto-calibrated"
            except ValueError:
                pass
        return 0.02, "fallback-0.02-unmeasured"

    def _stall_state(self, delta: float | None = None) -> dict:
        """Stall / exploration / prune snapshot for the round about to
        start (RRSI loop.py step 2-3: sigma_t, T_t, U_t, E_t, B_t).

        The trajectory is the incumbent score after each completed round
        (history.trajectory()); the current incumbent (frontier score, or
        the last trajectory point) is appended as index t, so stall_flag
        compares "now" with w rounds ago, exactly as RRSI's
        ``stall_flag(traj, t, cfg.w, delta)``. With fewer than w completed
        rounds stall is 0 by construction.
        """
        if delta is None:
            delta, _ = self._resolve_delta(allow_calibrate=False)
        traj = self.history.trajectory()
        now = self.frontier().get("score")
        if now is None:
            now = traj[-1] if traj else 0.0
        extended = traj + [float(now)]
        t = len(extended) - 1
        stall = stall_flag(extended, t, self.cfg.w, delta)
        tried = self.history.tried()
        return {"trajectory": extended, "t": t, "stall": stall,
                "tried": sorted(tried),
                "exploration": exploration(t, stall, tried,
                                           self.cfg.m_draft,
                                           self.domain_components),
                "prune_set": self.history.prune_set(t, self.cfg.n_prune)}

    def _eligible_tasks(self, apply_stall: bool) -> list[dict]:
        """Tasks still open, in file order. A task is open when it was
        never attempted, or its latest verdict is NEEDS_HUMAN with a
        human resolution recorded for that round (see pick_next_task).
        When ``apply_stall`` and the run is stalled, tasks whose declared
        component is stuck (same component failed >= 2 times in a row)
        are held back -- they are still open, just not picked now."""
        tried = self.history.attempted_tasks()
        stuck = set()
        if apply_stall:
            state = self._stall_state()
            if state["stall"]:
                stuck = self.history.stuck_components()
        out = []
        for t in self.tasks():
            tid = t.get("task_id")
            if tid in tried:
                # A NEEDS_HUMAN task is still open: it becomes retryable
                # once a human has recorded a resolution for that round's
                # candidate (e.g. "update-test-expectation: ..."). Without
                # a resolution the loop must not spin on the same task.
                if self.history.latest_verdict(tid) == "NEEDS_HUMAN":
                    rec = self.history.latest_record(tid)
                    if not (rec and self.feedback_log.has_resolution(
                            rec.get("candidate_id", ""),
                            after_round=rec.get("round"))):
                        continue
                else:
                    continue
            if stuck and t.get("component") in stuck:
                continue
            out.append(t)
        return out

    def pick_next_task(self) -> dict | None:
        """Next open task, skipping stuck-component tasks while stalled.

        When the run is stalled, tasks on a stuck component (same label
        failed >= 2 times in a row) are pushed back and the next task on
        a different component is picked instead. When every open task is
        on a stuck component this returns None -- it never invents a
        task. (run() then reports STALLED rather than DONE, so the
        difference between "nothing left" and "everything left is stuck"
        stays visible.)
        """
        tasks = self._eligible_tasks(apply_stall=True)
        return tasks[0] if tasks else None

    def status(self) -> dict:
        needs_human = []
        for t in self.tasks():
            tid = t.get("task_id")
            if self.history.latest_verdict(tid) == "NEEDS_HUMAN":
                rec = self.history.latest_record(tid)
                if rec and not self.feedback_log.has_resolution(
                        rec.get("candidate_id", ""),
                        after_round=rec.get("round")):
                    needs_human.append(tid)
        state = self._stall_state()
        delta, delta_source = self._resolve_delta(allow_calibrate=False)
        return {"frontier": self.frontier(),
                "history": self.history.render(20),
                "stop_requested": self.stop_requested(),
                "delta": delta,
                "delta_source": delta_source,
                "needs_human": needs_human,
                "feedback": {"human": len(self.feedback_log.records()),
                             "telemetry": len(self.telemetry_log.records())},
                "component_summary": self.history.component_summary(),
                "stall": state["stall"],
                "untried_components": state["exploration"]["untried"],
                "prune_set": state["prune_set"]}

    def _verifier_owned_markers(self, task: dict) -> tuple[str, ...]:
        """File names the verifier owns for this task (repro + held-out).

        Used as leakage markers: feedback mentioning one of these files, or
        a Fixer brief containing one, must be filtered / rejected. The
        names come from the task's tier A repro and tier C held-out dirs,
        which live outside any worktree.
        """
        markers: list[str] = []
        base = self.tasks_file.parent.parent
        for tier in ("A", "C"):
            spec = task.get("tiers", {}).get(tier, {})
            for key in ("repro", "heldout"):
                src = (spec.get(key) or {}).get("src")
                if not src:
                    continue
                d = Path(src) if Path(src).is_absolute() else base / src
                if d.is_dir():
                    markers += [f.name for f in d.iterdir() if f.is_file()]
        return tuple(markers)

    def delayed_recheck(self, task: dict, candidate_id: str,
                        after_hours: int = 24) -> dict:
        """Layer 1 delayed recheck: re-run the verifier at the adopted commit.

        Runs tiers A/B/C in a fresh worktree at the frontier (adopted)
        commit and appends the outcome to telemetry.jsonl. A passing
        recheck is a "correct" signal; a failing one is "introduced_bug"
        (the adoption did not hold). Returns the outcome dict.
        """
        task_id = task.get("task_id", "?")
        commit = self.frontier().get("baseline_commit")
        if not commit:
            return {"candidate_id": candidate_id, "task_id": task_id,
                    "passed": False,
                    "reason": "no adopted baseline to recheck"}
        judge_wt = self.runs_dir / f"judge-{task_id}-delayed"
        wt.create_worktree(self.repo, judge_wt, f"rsi/{task_id}-delayed",
                           start_point=commit)
        wt.provision_node_modules(judge_wt)
        try:
            reward = run_verifier(
                judge_wt, task, self.runs_dir / f"evidence-{task_id}-delayed",
                heldout_base=self.tasks_file.parent.parent,
                k=max(1, int(task.get("k", self.cfg.k))))
        finally:
            wt.remove_worktree(self.repo, judge_wt, f"rsi/{task_id}-delayed")
        label = "correct" if reward.passed else "introduced_bug"
        rec = self.telemetry_log.append(fb.FeedbackRecord(
            candidate_id=candidate_id, task_id=task_id,
            kind="delayed_recheck", source="verifier", label=label,
            note=(f"延迟复核（采纳后 {after_hours}h）：验证器 "
                  f"{'全过，采纳成立' if reward.passed else '未过，采纳不成立'}"),
            evidence={"score": reward.score, "passed": reward.passed,
                      "after_hours": after_hours}))
        return {"candidate_id": candidate_id, "task_id": task_id,
                "passed": reward.passed, "score": reward.score,
                "label": label, "record": rec}

    # ------------------------------------------------------------ round ---
    def run_task(self, task: dict, round_no: int) -> dict:
        """Run one task end to end. Returns the round summary dict."""
        task_id = task.get("task_id", "?")
        t0 = time.time()
        base_commit = self.baseline_commit()
        summary: dict = {"round": round_no, "task_id": task_id,
                         "baseline_commit": base_commit}
        # Stall / exploration / prune for this round (RRSI loop.py 2-3).
        # The exploration text goes to the Fixer with the brief (see
        # below); the compact stall/prune note goes into this round's
        # ledger detail, and the full text when stalled (render_brief in
        # fixer.py has no exploration slot and fixer.py is not this
        # component's file to change, so the ledger is the durable copy).
        # k: the task's own "k" wins, else the run config's k (default 1);
        # only checks the task names in flaky_checks run k times.
        k_eff = max(1, int(task.get("k", self.cfg.k)))
        summary["k"] = k_eff
        # Provisional delta for the stall snapshot: config, calibration
        # file, or the 0.02 fallback. It is re-resolved after BASELINE
        # (fresh baseline evaluation recorded), before judge().
        delta, delta_source = self._resolve_delta(allow_calibrate=False)
        summary["delta"] = delta
        summary["delta_source"] = delta_source
        state = self._stall_state(delta=delta)
        summary["stall"] = state["stall"]
        summary["exploration"] = state["exploration"]["text"]
        summary["untried_components"] = state["exploration"]["untried"]
        summary["prune_set"] = state["prune_set"]
        _note = ""
        if state["stall"] or state["prune_set"]:
            _note = f" | stall={state['stall']}"
            if state["prune_set"]:
                _note += (" prune=" + ",".join(
                    p["component"] for p in state["prune_set"]))
            if state["stall"]:
                _note += " exploration: " + state["exploration"]["text"]

        def _detail(base: str) -> str:
            return base + _note if _note else base

        # Component label: the task's declared label until a diff exists,
        # then the label the diff evidences (components.normalize --
        # a declared label without diff evidence is re-attributed).
        comp = task.get("component")

        # BASELINE: does the bug reproduce on a clean tree? Runs all tiers
        # (fail_fast=False) to capture the full per-test map: tier B's P2P
        # set and tier C's held-out baseline.
        judge_base = self.runs_dir / f"judge-{task_id}-base"
        wt.create_worktree(self.repo, judge_base, f"rsi/{task_id}-base",
                           start_point=base_commit)
        wt.provision_node_modules(judge_base)
        try:
            base_reward = run_verifier(
                judge_base, task, self.runs_dir / f"evidence-{task_id}-base",
                heldout_base=self.tasks_file.parent.parent, fail_fast=False,
                k=k_eff)
        finally:
            wt.remove_worktree(self.repo, judge_base, f"rsi/{task_id}-base")
        summary["baseline_score"] = base_reward.score
        summary["baseline_passed"] = base_reward.passed
        # Persist the baseline measurement: the reward file is what the
        # deterministic analyzer reads for flaky-test detection (the
        # ledger only keeps tier summaries), and the recorded baseline
        # evaluation is what `calibrate` re-estimates delta from.
        base_reward.save(self.runs_dir / f"reward-{task_id}-base.json")
        base_ev = sel.eval_from_reward(task_id, base_reward, "base",
                                       k=k_eff)
        record_baseline_eval(self.runs_dir, base_ev, task_id)
        # Fresh baseline data may now calibrate delta: re-resolve before
        # judge() so an unmeasured delta does not crash the round.
        delta, delta_source = self._resolve_delta(allow_calibrate=True)
        summary["delta"] = delta
        summary["delta_source"] = delta_source
        if base_reward.passed:
            self.history.append_candidate(
                round_no, task_id, f"{task_id}-base", "base", None,
                "bug does not reproduce on baseline", None,
                {"A": base_reward.tiers.get("A", {})}, "SKIPPED",
                base_commit, base_reward.score, None,
                round(time.time() - t0, 1), fixer=self.fixer.name,
                detail=_detail("baseline already passes; nothing to fix"),
                components=[comp] if comp else [])
            summary["verdict"] = "SKIPPED"
            summary["detail"] = "bug does not reproduce on baseline"
            return summary

        # FIX: the fixer works in its own worktree. It gets sanitised,
        # prior-round feedback only (label + one-line note, no evidence, no
        # held-out content -- rsi/feedback.py::feedback_for_fixer), plus
        # the leakage markers so its brief can be checked. It never sees
        # the feedback log or the held-out tests.
        fixer_wt = self.runs_dir / f"fixer-{task_id}"
        wt.create_worktree(self.repo, fixer_wt, f"rsi/{task_id}-fixer",
                           start_point=base_commit)
        # Worktree excludes (self domain: the task material). The Fixer
        # must not be able to read its own exam: the excluded paths are
        # removed from its worktree AND from the diff taken afterwards
        # (diff_of_worktree's exclude pathspec), so neither reading nor
        # re-creating them can reach the Judge. The task itself still
        # reaches the Fixer through the brief, never through these files.
        for rel in self.worktree_excludes:
            target = fixer_wt / rel
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        # No node_modules here in the pilot: the manual fixer runs no
        # commands. A real fixer that self-checks with tsc/vitest must
        # provision them (wt.provision_node_modules) when it is wired.
        task_for_fixer = dict(task)
        task_for_fixer["prior_feedback"] = fb.feedback_for_fixer(
            self.feedback_log, task_id, round_no,
            heldout_markers=self._verifier_owned_markers(task))
        task_for_fixer["heldout_markers"] = list(
            self._verifier_owned_markers(task))
        # Stall redirection, carried with the brief: the exploration text
        # (and the stall flag + prune set) ride on the task dict -- the
        # fixer interface (fix(task, worktree, budget)) is unchanged, and
        # render_brief in fixer.py renders only description / repro /
        # feedback, so the dict is the carrier until its owner adds a
        # slot. The same text is also recorded in this round's ledger
        # detail (see _detail) and in the round summary.
        task_for_fixer["exploration"] = state["exploration"]["text"]
        task_for_fixer["stall_flag"] = state["stall"]
        task_for_fixer["prune_set"] = [p["component"]
                                       for p in state["prune_set"]]
        try:
            result: FixResult = self.fixer.fix(
                task_for_fixer, fixer_wt,
                {"max_submissions": self.cfg.max_submissions,
                 "token_budget": self.cfg.token_budget,
                 "time_budget_s": self.cfg.time_budget_s})
        except Exception as e:  # fixer failure is STUCK, not a crash
            wt.remove_worktree(self.repo, fixer_wt, f"rsi/{task_id}-fixer")
            self.history.append_candidate(
                round_no, task_id, f"{task_id}-c1", "A", None,
                task.get("hypothesis"), None, {}, "STUCK", base_commit, None,
                None, round(time.time() - t0, 1), fixer=self.fixer.name,
                detail=_detail(f"fixer raised: {e}"),
                components=[comp] if comp else [])
            summary["verdict"] = "STUCK"
            summary["detail"] = f"fixer raised: {e}"
            return summary
        diff = wt.diff_of_worktree(fixer_wt, base_commit,
                                   exclude=self.worktree_excludes or None)
        wt.remove_worktree(self.repo, fixer_wt, f"rsi/{task_id}-fixer")
        # From here on the label is what the diff evidences, not what the
        # task declared (components.normalize: an unevidenced declaration
        # is re-attributed from the diff's file/content signals).
        comp = normalize_component(task.get("component"), diff)
        summary["submissions"] = result.submissions
        if not diff.strip():
            self.history.append_candidate(
                round_no, task_id, f"{task_id}-c1", "A", None,
                task.get("hypothesis"), diff, {}, "STUCK", base_commit, None,
                result.cost_tokens, result.cost_seconds, fixer=self.fixer.name,
                detail=_detail("fixer produced an empty diff"),
                components=[comp])
            summary["verdict"] = "STUCK"
            summary["detail"] = "fixer produced an empty diff"
            return summary

        # VERIFY: critic first, then a clean Judge worktree. The domain
        # selects the domain-scoped blacklist (domain="self" turns on the
        # meta-guardrail) and the domain's own critic patterns apply on
        # top of the generic protected list (RRSI: domain patterns).
        review = critic_mod.review(
            diff, domain_patterns=self.domain_patterns or None,
            task_ids=[t.get("task_id") for t in self.tasks()],
            domain=self.domain)
        if review["verdict"] == "reject":
            verdict = "GAMING-DETECTED" if review["gaming"] else "REJECT"
            self.history.append_candidate(
                round_no, task_id, f"{task_id}-c1", "A", None,
                task.get("hypothesis"), diff, {}, verdict, base_commit, None,
                result.cost_tokens, result.cost_seconds, fixer=self.fixer.name,
                detail=_detail("; ".join(review["reasons"])),
                components=[comp])
            summary["verdict"] = verdict
            summary["detail"] = "; ".join(review["reasons"])
            return summary

        judge_wt = self.runs_dir / f"judge-{task_id}"
        # A previous NEEDS_HUMAN round leaves branch rsi/<task-id> behind
        # for human review; a re-run (after a human resolution) supersedes
        # it, so drop the stale branch first. The ledger record and the
        # approval card file are the durable record of that round.
        wt._git(self.repo, "branch", "-D", f"rsi/{task_id}", check=False)
        wt.create_worktree(self.repo, judge_wt, f"rsi/{task_id}",
                           start_point=base_commit)
        wt.provision_node_modules(judge_wt)
        try:
            wt.apply_diff(judge_wt, diff)
            reward: Reward = run_verifier(
                judge_wt, task, self.runs_dir / f"evidence-{task_id}",
                heldout_base=self.tasks_file.parent.parent, k=k_eff)
        except Exception as e:  # diff does not apply cleanly: STUCK
            wt.remove_worktree(self.repo, judge_wt, f"rsi/{task_id}")
            self.history.append_candidate(
                round_no, task_id, f"{task_id}-c1", "A", task.get("component"),
                task.get("hypothesis"), diff, {}, "STUCK", base_commit, None,
                result.cost_tokens, result.cost_seconds,
                fixer=self.fixer.name, detail=_detail(f"judge setup failed: {e}"),
                components=[comp])
            summary["verdict"] = "STUCK"
            summary["detail"] = f"judge setup failed: {e}"
            return summary
        reward.save(self.runs_dir / f"reward-{task_id}-c1.json")
        summary["score"] = reward.score
        summary["tiers"] = {k: {"passed": v["passed"], "status": v["status"]}
                            for k, v in reward.tiers.items()}

        # ADOPT / REJECT: selection (noise floor + cost rule + hard gate).
        # P2P is per-test (SWE-bench semantics): a test that passed at
        # baseline must still pass after the fix; a test that already failed
        # at baseline (e.g. Morde's environment-dependent DSH tests) is not
        # part of P2P. Without go-test-json data, fall back to the tier-B
        # passed flag.
        base_b = sel.test_map(base_reward, "B")
        cand_b = sel.test_map(reward, "B")
        if base_b and cand_b:
            p2p_clean = all(cand_b.get(t, False) for t, ok in base_b.items()
                            if ok)
        else:
            p2p_clean = None
        tier_a_pass = reward.tiers.get("A", {}).get("passed", False)
        tier_c_pass = reward.tiers.get("C", {}).get("passed", False)

        # NEEDS_HUMAN routing (CAND-001 lesson): a single newly-failing
        # test with tiers A and C green may be a test that encoded the
        # buggy behaviour, not a real regression. The P2P gate cannot tell
        # them apart, so this is routed to a human instead of auto-REJECT.
        # With a per-test map the classification is exact; without one
        # (plain vitest tier B) the failing file is read from the tier-B
        # evidence logs and matched against the task's declared
        # behavior_tests. Unattributable failures stay clear regressions.
        if base_b and cand_b:
            classification = sel.classify_p2p_failure(
                base_b, cand_b, tier_a_pass=tier_a_pass,
                tier_c_pass=tier_c_pass,
                related_tests=(set(task.get("behavior_tests") or [])
                               if task.get("behavior_tests") else None))
        elif (not reward.tiers.get("B", {}).get("passed", True)
                and tier_a_pass and tier_c_pass):
            failed_files = _failed_files_from_evidence(
                self.runs_dir / f"evidence-{task_id}", "B")
            declared = task.get("behavior_tests") or []
            if (len(failed_files) == 1 and declared
                    and any(d in failed_files[0] or failed_files[0] in d
                            for d in declared)):
                classification = {
                    "kind": "ambiguous_single", "failed": failed_files,
                    "reason": (f"single failing file {failed_files[0]!r} "
                               "asserts the changed behaviour (tiers A and "
                               "C green); the test may encode the bug")}
            else:
                classification = {
                    "kind": "clear_regression", "failed": failed_files,
                    "reason": "tier B failed without an attributable single point"}
        else:
            classification = {"kind": "clean", "failed": [],
                              "reason": "no pass-to-pass failure"}
        if classification["kind"] == "ambiguous_single":
            # Keep the branch for human review, write the approval card,
            # leave the frontier untouched. The Fixer must never resolve
            # this by editing the test: changing a test expectation is a
            # human edit, outside the loop.
            wt.commit_worktree(
                judge_wt, f"rsi: {task_id} (candidate A, NEEDS_HUMAN)")
            wt.remove_worktree(self.repo, judge_wt)  # keep branch rsi/<task-id>
            checks = [c for t in reward.tiers.values()
                      if t.get("status") == "run" for c in t.get("checks", [])]
            n_pass = sum(1 for c in checks if c.get("passed"))
            cost_ok = (self.cfg.token_budget is None
                       or result.cost_tokens is None
                       or result.cost_tokens <= self.cfg.token_budget)
            vote = fb.three_vote_decision(
                verifier_ok=None, user_label=None, negative_streak=0,
                cost_ok=cost_ok)
            card = fb.build_approval_card(
                candidate_id=f"{task_id}-c1", task_id=task_id,
                behavior_change=(task.get("behavior_change")
                                 or task.get("title")
                                 or task.get("hypothesis") or task_id),
                tests_passed=n_pass, tests_total=len(checks),
                tiers={k: v.get("passed", False)
                       for k, v in reward.tiers.items()},
                cost_tokens=result.cost_tokens,
                cost_seconds=result.cost_seconds + reward.duration_s,
                score_before=base_reward.score, score_after=reward.score,
                failed_tests=classification["failed"],
                needs_human_reason=classification["reason"],
                branch=f"rsi/{task_id}")
            card_path = self.runs_dir / f"approval-{task_id}-c1.json"
            card_path.write_text(json.dumps(card, indent=1,
                                            ensure_ascii=False))
            self.history.append_candidate(
                round_no, task_id, f"{task_id}-c1", "A",
                comp, task.get("hypothesis"), diff,
                {k: {"passed": v["passed"], "status": v["status"]}
                 for k, v in reward.tiers.items()},
                "NEEDS_HUMAN", base_commit, reward.score,
                result.cost_tokens,
                result.cost_seconds + reward.duration_s,
                fixer=self.fixer.name,
                detail=_detail(classification["reason"]
                               + f" card: {card_path.name}"),
                components=[comp])
            summary["verdict"] = "NEEDS_HUMAN"
            summary["detail"] = classification["reason"]
            summary["decision"] = vote
            summary["approval_card"] = str(card_path)
            summary["approval_card_text"] = fb.render_approval_card_text(card)
            summary["failed_tests"] = classification["failed"]
            summary["branch"] = f"rsi/{task_id}"
            summary["duration_s"] = round(time.time() - t0, 1)
            return summary

        # base_ev was computed right after BASELINE (and recorded for
        # calibration); only the candidate side is evaluated here.
        cand_ev = sel.eval_from_reward(task_id, reward, "candidate",
                                       p2p_clean=p2p_clean, k=k_eff)
        s_star = max(self.frontier().get("score") or 0.0, base_ev.S)
        cand = sel.Candidate(variant="A",
                             edits=[{"component": comp,
                                     "hypothesis": task.get("hypothesis")}],
                             ev=cand_ev)
        incumbent_counts = {}
        for r in self.history.accepted():
            c = r.get("component")
            if c:
                incumbent_counts[c] = incumbent_counts.get(c, 0) + 1
        guards = self.guard_fn(diff) if self.guard_fn else []
        # delta is the value resolved after BASELINE (config, measured
        # calibration, auto-calibrated, or the 0.02 fallback) -- never
        # None here, so judge() cannot crash on an unmeasured delta.
        decision = sel.judge(cand, base_ev, s_star, delta,
                             self.cfg, incumbent_counts, guards)
        summary["decision"] = decision.to_json()
        if decision.admissible:
            commit = wt.commit_worktree(
                judge_wt, f"rsi: {task_id} (candidate A, score {reward.score})")
            # Remove the worktree but KEEP the branch rsi/<task-id>: it is
            # the delivery artifact for human review. Never merge it here.
            wt.remove_worktree(self.repo, judge_wt)
            self._save_frontier({"baseline_commit": commit,
                                 "score": reward.score,
                                 "task_id": task_id,
                                 "candidate_id": f"{task_id}-c1",
                                 "n_accepted": len(self.history.accepted()) + 1})
            verdict = "ADOPT"
        else:
            wt.remove_worktree(self.repo, judge_wt, f"rsi/{task_id}")
            verdict = "REJECT"
        self.history.append_candidate(
            round_no, task_id, f"{task_id}-c1", "A", comp,
            task.get("hypothesis"), diff,
            {k: {"passed": v["passed"], "status": v["status"]}
             for k, v in reward.tiers.items()},
            verdict, self.frontier().get("baseline_commit"), reward.score,
            result.cost_tokens,
            result.cost_seconds + reward.duration_s,
            fixer=self.fixer.name, detail=_detail(decision.reason),
            components=[comp])
        summary["verdict"] = verdict
        summary["detail"] = decision.reason
        summary["duration_s"] = round(time.time() - t0, 1)
        return summary

    # -------------------------------------------------------------- run ---
    def run(self, start: int = 0) -> list[dict]:
        """Run rounds until T, tasks exhausted, or STOP. Returns summaries."""
        summaries = []
        for t in range(start, self.cfg.T):
            if self.stop_requested():
                summaries.append({"round": t, "verdict": "STOPPED",
                                  "detail": "STOP file present"})
                break
            task = self.pick_next_task()
            if task is None:
                if self._eligible_tasks(apply_stall=False):
                    # Open tasks exist, but every one of them is on a
                    # stuck component while stalled: the loop must not
                    # report DONE (nothing left) and must not invent a
                    # task. STALLED says "wait for a human to unstick a
                    # component or add a task on another one".
                    summaries.append({"round": t, "verdict": "STALLED",
                                      "detail": ("stalled: all remaining "
                                                 "tasks are on stuck "
                                                 "components")})
                else:
                    summaries.append({"round": t, "verdict": "DONE",
                                      "detail": "no more tasks"})
                break
            summaries.append(self.run_task(task, t))
        return summaries
