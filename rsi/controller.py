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
import time
from pathlib import Path

from . import critic as critic_mod
from . import feedback as fb
from . import selection as sel
from . import worktree as wt
from .config import RSIConfig
from .fixer import FixResult
from .history import History
from .verifier import Reward, run_verifier


def _failed_files_from_evidence(evidence_dir: Path | str,
                                tier: str = "B") -> list[str]:
    """Failing test files named in a tier's evidence logs (vitest/jest style).

    The per-test P2P map only exists for go-test-json tiers. Portal tiers
    run plain `vitest run`, whose evidence log prints lines like
    ``FAIL  web/portal/src/routes/Projects.test.tsx``. This parser reads
    those lines so the NEEDS_HUMAN router can still attribute a lone
    failure. Returns [] when nothing parseable is found (caller must then
    treat the failure as unattributable, i.e. a clear regression, not an
    ambiguous one).
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
    return sorted(out)


class Controller:
    def __init__(self, repo: Path | str, runs_dir: Path | str,
                 tasks_file: Path | str, config: RSIConfig,
                 fixer=None, history: History | None = None,
                 skill_md: str = "", guard_fn=None,
                 feedback_log: fb.FeedbackLog | None = None,
                 telemetry_log: fb.FeedbackLog | None = None):
        self.repo = Path(repo)
        self.runs_dir = Path(runs_dir)
        self.tasks_file = Path(tasks_file)
        self.cfg = config
        self.fixer = fixer
        self.skill_md = skill_md
        self.guard_fn = guard_fn  # callable diff -> list[str] violations
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

    def pick_next_task(self) -> dict | None:
        tried = self.history.attempted_tasks()
        for t in self.tasks():
            tid = t.get("task_id")
            if tid not in tried:
                return t
            # A NEEDS_HUMAN task is still open: it becomes retryable once a
            # human has recorded a resolution for that round's candidate
            # (e.g. "update-test-expectation: ..."). Without a resolution
            # the loop must not spin on the same task.
            if self.history.latest_verdict(tid) == "NEEDS_HUMAN":
                rec = self.history.latest_record(tid)
                if rec and self.feedback_log.has_resolution(
                        rec.get("candidate_id", ""),
                        after_round=rec.get("round")):
                    return t
        return None

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
        return {"frontier": self.frontier(),
                "history": self.history.render(20),
                "stop_requested": self.stop_requested(),
                "needs_human": needs_human,
                "feedback": {"human": len(self.feedback_log.records()),
                             "telemetry": len(self.telemetry_log.records())}}

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
                heldout_base=self.tasks_file.parent.parent)
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
                heldout_base=self.tasks_file.parent.parent, fail_fast=False)
        finally:
            wt.remove_worktree(self.repo, judge_base, f"rsi/{task_id}-base")
        summary["baseline_score"] = base_reward.score
        summary["baseline_passed"] = base_reward.passed
        if base_reward.passed:
            self.history.append_candidate(
                round_no, task_id, f"{task_id}-base", "base", None,
                "bug does not reproduce on baseline", None,
                {"A": base_reward.tiers.get("A", {})}, "SKIPPED",
                base_commit, base_reward.score, None,
                round(time.time() - t0, 1), fixer=self.fixer.name,
                detail="baseline already passes; nothing to fix")
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
        # No node_modules here in the pilot: the manual fixer runs no
        # commands. A real fixer that self-checks with tsc/vitest must
        # provision them (wt.provision_node_modules) when it is wired.
        task_for_fixer = dict(task)
        task_for_fixer["prior_feedback"] = fb.feedback_for_fixer(
            self.feedback_log, task_id, round_no,
            heldout_markers=self._verifier_owned_markers(task))
        task_for_fixer["heldout_markers"] = list(
            self._verifier_owned_markers(task))
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
                detail=f"fixer raised: {e}")
            summary["verdict"] = "STUCK"
            summary["detail"] = f"fixer raised: {e}"
            return summary
        diff = wt.diff_of_worktree(fixer_wt, base_commit)
        wt.remove_worktree(self.repo, fixer_wt, f"rsi/{task_id}-fixer")
        summary["submissions"] = result.submissions
        if not diff.strip():
            self.history.append_candidate(
                round_no, task_id, f"{task_id}-c1", "A", None,
                task.get("hypothesis"), diff, {}, "STUCK", base_commit, None,
                result.cost_tokens, result.cost_seconds, fixer=self.fixer.name,
                detail="fixer produced an empty diff")
            summary["verdict"] = "STUCK"
            summary["detail"] = "fixer produced an empty diff"
            return summary

        # VERIFY: critic first, then a clean Judge worktree.
        review = critic_mod.review(
            diff, domain_patterns=None,
            task_ids=[t.get("task_id") for t in self.tasks()])
        if review["verdict"] == "reject":
            verdict = "GAMING-DETECTED" if review["gaming"] else "REJECT"
            self.history.append_candidate(
                round_no, task_id, f"{task_id}-c1", "A", None,
                task.get("hypothesis"), diff, {}, verdict, base_commit, None,
                result.cost_tokens, result.cost_seconds, fixer=self.fixer.name,
                detail="; ".join(review["reasons"]))
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
                heldout_base=self.tasks_file.parent.parent)
        except Exception as e:  # diff does not apply cleanly: STUCK
            wt.remove_worktree(self.repo, judge_wt, f"rsi/{task_id}")
            self.history.append_candidate(
                round_no, task_id, f"{task_id}-c1", "A", task.get("component"),
                task.get("hypothesis"), diff, {}, "STUCK", base_commit, None,
                result.cost_tokens, result.cost_seconds,
                fixer=self.fixer.name, detail=f"judge setup failed: {e}")
            summary["verdict"] = "STUCK"
            summary["detail"] = f"judge setup failed: {e}"
            return summary
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
                task.get("component"), task.get("hypothesis"), diff,
                {k: {"passed": v["passed"], "status": v["status"]}
                 for k, v in reward.tiers.items()},
                "NEEDS_HUMAN", base_commit, reward.score,
                result.cost_tokens,
                result.cost_seconds + reward.duration_s,
                fixer=self.fixer.name,
                detail=classification["reason"] + f" card: {card_path.name}")
            summary["verdict"] = "NEEDS_HUMAN"
            summary["detail"] = classification["reason"]
            summary["decision"] = vote
            summary["approval_card"] = str(card_path)
            summary["approval_card_text"] = fb.render_approval_card_text(card)
            summary["failed_tests"] = classification["failed"]
            summary["branch"] = f"rsi/{task_id}"
            summary["duration_s"] = round(time.time() - t0, 1)
            return summary

        base_ev = sel.eval_from_reward(task_id, base_reward, "base")
        cand_ev = sel.eval_from_reward(task_id, reward, "candidate",
                                       p2p_clean=p2p_clean)
        s_star = max(self.frontier().get("score") or 0.0, base_ev.S)
        cand = sel.Candidate(variant="A",
                             edits=[{"component": task.get("component"),
                                     "hypothesis": task.get("hypothesis")}],
                             ev=cand_ev)
        incumbent_counts = {}
        for r in self.history.accepted():
            c = r.get("component")
            if c:
                incumbent_counts[c] = incumbent_counts.get(c, 0) + 1
        guards = self.guard_fn(diff) if self.guard_fn else []
        decision = sel.judge(cand, base_ev, s_star, self.cfg.delta,
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
            round_no, task_id, f"{task_id}-c1", "A", task.get("component"),
            task.get("hypothesis"), diff,
            {k: {"passed": v["passed"], "status": v["status"]}
             for k, v in reward.tiers.items()},
            verdict, self.frontier().get("baseline_commit"), reward.score,
            result.cost_tokens,
            result.cost_seconds + reward.duration_s,
            fixer=self.fixer.name, detail=decision.reason)
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
                summaries.append({"round": t, "verdict": "DONE",
                                  "detail": "no more tasks"})
                break
            summaries.append(self.run_task(task, t))
        return summaries
