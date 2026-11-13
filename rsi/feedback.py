"""Feedback for morde-rsi: external signals the Fixer cannot see.

Layer 0 (the deterministic verifier, tiers A/B/C) is the main judge and
already exists in rsi/verifier.py. This module adds the two layers the
2026-11-13 design approved:

* Layer 1 -- silent telemetry (no user interruption). Signals are derived
  from observable behaviour, never from asking the user: a repeated intent
  (the user re-asks the same thing), a manual revert (the user edits back a
  file RSI changed), an approval-card rejection, a delayed recheck (re-run
  the verifier 24h / 7d after adoption), and a shadow compare (same input,
  baseline vs candidate). All of it lands in ``telemetry.jsonl`` under
  runs/, keyed by candidate_id / task_id, separate from reward.json and
  never overwriting it.
* Layer 2 -- one-tap approval card (Morde's card format, no new UI). A
  card shows what changed in *behaviour* (never a diff), how many tests
  passed, and what it cost; at most three actions: adopt / reject /
  misdirected. Success is silent by default (adopt into preview, don't
  ask); a card is only raised when the loop is uncertain, the fix looks
  misdirected, or the cost is over budget. Chat reactions (thumbs up/down)
  are single-use: the first tap is recorded, later taps are ignored.
* Layer 3 -- deep review is a STUB here (see request_deep_review): only
  the trigger conditions and a human checklist, no UI.

Rules that hold the whole module together:

* Feedback never rewrites reward. A human label and the verifier and the
  cost are three votes (see three_vote_decision); the verifier's
  fail-to-pass / held-out failure is a veto, and two of three votes are
  needed to adopt. A single thumbs-down pauses auto-adopt (NEEDS_HUMAN);
  two consecutive negatives reject.
* Feedback must become something executable (see feedback_to_learning):
  a new held-out test *spec* (draft -- a human writes the real test from
  it; a spec is never counted as a passing test) or a new evolve-task
  spec, or it is recorded as weight only. Vague feedback converts to
  nothing.
* The Fixer only ever sees *sanitised, prior-round* feedback
  (see feedback_for_fixer): label + one-line note, no evidence, no
  held-out content, nothing from the current round. Held-out material is
  checked against the brief by rsi/critic.py::review_brief.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Vocabulary. Labels are the five fixed human verdicts (Chinese display in
# FEEDBACK_LABELS); kinds are where a record came from.
# --------------------------------------------------------------------------

FEEDBACK_LABELS = {
    "correct": "修对",
    "wrong": "修错",
    "misdirected": "修偏",
    "missed_point": "没修到点",
    "introduced_bug": "引入新问题",
}
NEGATIVE_LABELS = frozenset(
    {"wrong", "misdirected", "missed_point", "introduced_bug"})

#: Kinds a feedback record may carry.
FEEDBACK_KINDS = frozenset({
    # Layer 1 (telemetry, no user action).
    "repeat_intent", "manual_revert", "approval_reject", "approval_approve",
    "delayed_recheck", "shadow_compare",
    # Layer 2 (one tap).
    "human_label", "chat_reaction",
    # Layer 3 / loop bookkeeping.
    "human_resolution",
})

#: Sources a record may come from. "telemetry" is derived behaviour;
#: "human" is a tap or a typed note; "verifier" is the delayed recheck.
SOURCES = frozenset({"telemetry", "human", "verifier", "system"})

#: Kinds that are negative even without an explicit label.
STRONG_NEGATIVE_KINDS = frozenset({"manual_revert", "approval_reject"})

#: Evidence keys that must never appear in a feedback record: held-out
#: content and reward files are verifier material, not feedback material.
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {"heldout_content", "heldout_test", "heldout", "reward_json"})

MAX_NOTE_LEN = 500

CARD_ACTIONS = [
    {"id": "adopt", "label": "采纳"},
    {"id": "reject", "label": "驳回"},
    {"id": "misdirected", "label": "修偏了"},
]


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Records + log. One JSONL record per feedback event, separate files from
# history.jsonl (the candidate ledger) and reward.json (the verifier).
# --------------------------------------------------------------------------

@dataclass
class FeedbackRecord:
    candidate_id: str
    task_id: str
    kind: str
    note: str
    source: str = "human"
    label: str | None = None
    round_no: int | None = None
    evidence: dict = field(default_factory=dict)
    ts: str = ""

    def validate(self) -> list[str]:
        """Return a list of problems; [] means the record is usable."""
        errs: list[str] = []
        if not self.candidate_id or not isinstance(self.candidate_id, str):
            errs.append("candidate_id is required")
        if not self.task_id or not isinstance(self.task_id, str):
            errs.append("task_id is required")
        if self.kind not in FEEDBACK_KINDS:
            errs.append(f"unknown kind: {self.kind!r}")
        if self.source not in SOURCES:
            errs.append(f"unknown source: {self.source!r}")
        if self.label is not None and self.label not in FEEDBACK_LABELS:
            errs.append(f"unknown label: {self.label!r}")
        if not isinstance(self.note, str) or not self.note.strip():
            errs.append("note is required (one sentence: what happened)")
        elif len(self.note) > MAX_NOTE_LEN:
            errs.append(f"note is longer than {MAX_NOTE_LEN} chars")
        if not isinstance(self.evidence, dict):
            errs.append("evidence must be a dict")
        else:
            bad = sorted(set(self.evidence) & FORBIDDEN_EVIDENCE_KEYS)
            if bad:
                errs.append(f"evidence must not carry verifier material: {bad}")
        return errs

    def to_json(self) -> dict:
        d = asdict(self)
        if not d.get("ts"):
            d["ts"] = _ts()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "FeedbackRecord":
        return cls(
            candidate_id=d.get("candidate_id", ""),
            task_id=d.get("task_id", ""),
            kind=d.get("kind", ""),
            note=d.get("note", ""),
            source=d.get("source", "human"),
            label=d.get("label"),
            round_no=d.get("round_no"),
            evidence=dict(d.get("evidence") or {}),
            ts=d.get("ts", ""),
        )


def polarity(rec: FeedbackRecord | dict) -> int:
    """+1 positive, -1 negative, 0 neutral (no usable signal).

    A negative label is negative; "correct" is positive. Telemetry kinds
    that are negative by construction (manual revert, approval rejection)
    count as negative even with no label. Shadow compares and vague
    records are neutral: a behaviour difference alone is not a verdict.
    """
    label = rec.label if isinstance(rec, FeedbackRecord) else rec.get("label")
    kind = rec.kind if isinstance(rec, FeedbackRecord) else rec.get("kind")
    if label == "correct":
        return 1
    if label in NEGATIVE_LABELS:
        return -1
    if kind in STRONG_NEGATIVE_KINDS:
        return -1
    return 0


class FeedbackLog:
    """Append-only JSONL log of feedback records (one file per runs dir).

    The controller keeps two of these: ``human_feedback.jsonl`` (layer 2/3,
    what a human tapped or typed) and ``telemetry.jsonl`` (layer 1, derived
    behaviour). Both are separate from history.jsonl and reward.json.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def append(self, rec: FeedbackRecord | dict) -> dict:
        """Validate and append one record. Raises ValueError if invalid."""
        d = rec.to_json() if isinstance(rec, FeedbackRecord) else dict(rec)
        errs = FeedbackRecord.from_json(d).validate()
        if errs:
            raise ValueError("invalid feedback record: " + "; ".join(errs))
        if not d.get("ts"):
            d["ts"] = _ts()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
        return d

    def by_candidate(self, candidate_id: str) -> list[dict]:
        return [r for r in self.records()
                if r.get("candidate_id") == candidate_id]

    def by_task(self, task_id: str) -> list[dict]:
        return [r for r in self.records() if r.get("task_id") == task_id]

    def negative_streak(self, candidate_id: str) -> int:
        """Trailing consecutive negatives for one candidate.

        A positive record resets the streak to 0; a neutral record leaves
        it unchanged. One thumbs-down therefore pauses (streak 1) but does
        not kill; two in a row do.
        """
        streak = 0
        for r in self.by_candidate(candidate_id):
            p = polarity(r)
            if p < 0:
                streak += 1
            elif p > 0:
                streak = 0
        return streak

    def has_resolution(self, candidate_id: str,
                       after_round: int | None = None) -> bool:
        """Has a human resolved this candidate (so its task may be retried)?

        `after_round` ties the resolution to one NEEDS_HUMAN round: a
        resolution recorded for round N does not requeue a *later* round's
        NEEDS_HUMAN for the same candidate id. A resolution with no round
        number counts for any round (it was recorded by hand).
        """
        for r in self.by_candidate(candidate_id):
            if r.get("kind") != "human_resolution":
                continue
            rn = r.get("round_no")
            if after_round is None or rn is None or rn >= after_round:
                return True
        return False

    def pending_human(self) -> list[dict]:
        """Candidates with a negative signal and no human resolution yet."""
        out = []
        for r in self.records():
            if polarity(r) < 0 and not self.has_resolution(r["candidate_id"]):
                out.append(r)
        return out


# --------------------------------------------------------------------------
# Layer 1: silent telemetry. Pure functions over plain data (no Morde repo,
# no network): the caller supplies the observations, these decide whether
# they amount to a feedback signal.
# --------------------------------------------------------------------------

def normalize_intent(text: str) -> str:
    """Normalise an intent for repeat detection: case/space/punct-insensitive."""
    return re.sub(r"\s+", "", re.sub(r"[^\w]", "", text.lower()))


def detect_repeat_intent(prior_intents: list[str], new_intent: str, *,
                         candidate_id: str, task_id: str,
                         round_no: int | None = None) -> FeedbackRecord | None:
    """Same intent re-asked after a fix shipped: the fix missed the point.

    Returns a record, or None when the new intent is genuinely new.
    """
    norm = normalize_intent(new_intent)
    if norm and any(normalize_intent(p) == norm for p in prior_intents):
        return FeedbackRecord(
            candidate_id=candidate_id, task_id=task_id,
            kind="repeat_intent", source="telemetry",
            label="missed_point",
            note=f"用户重复提出同一意图：{new_intent[:80]}",
            round_no=round_no,
            evidence={"intent": new_intent[:120]})
    return None


def detect_manual_revert(candidate_files: list[str],
                         user_changed_files: list[str], *,
                         candidate_id: str, task_id: str,
                         round_no: int | None = None) -> FeedbackRecord | None:
    """The user hand-edited (or reverted) a file RSI had just changed.

    A strong negative signal: the fix was not wanted as shipped. Returns a
    record, or None when there is no overlap.
    """
    overlap = sorted(set(candidate_files) & set(user_changed_files))
    if not overlap:
        return None
    shown = ", ".join(overlap[:3])
    extra = f" 等 {len(overlap)} 个文件" if len(overlap) > 3 else ""
    return FeedbackRecord(
        candidate_id=candidate_id, task_id=task_id,
        kind="manual_revert", source="telemetry", label="wrong",
        note=f"用户手动改回了 RSI 改过的文件：{shown}{extra}",
        round_no=round_no, evidence={"reverted_files": overlap})


def approval_record(outcome: str, *, candidate_id: str, task_id: str,
                    round_no: int | None = None) -> FeedbackRecord | None:
    """Turn an approval-card outcome into a feedback record.

    "rejected" -> wrong; "approved" -> correct (a weak positive);
    anything else -> None (no signal, e.g. the card is still pending).
    """
    if outcome == "rejected":
        return FeedbackRecord(
            candidate_id=candidate_id, task_id=task_id,
            kind="approval_reject", source="telemetry", label="wrong",
            note="审批卡被驳回", round_no=round_no)
    if outcome == "approved":
        return FeedbackRecord(
            candidate_id=candidate_id, task_id=task_id,
            kind="approval_approve", source="telemetry", label="correct",
            note="审批卡通过", round_no=round_no)
    return None


def shadow_compare(baseline: dict[str, str], candidate: dict[str, str], *,
                   candidate_id: str, task_id: str,
                   round_no: int | None = None) -> FeedbackRecord:
    """Compare baseline vs candidate outputs on the SAME inputs.

    This is only the comparator. Feeding it real outputs (running both
    versions) is a future adapter -- the same shape as the DSHFixer stub.
    A mismatch is evidence, not a verdict, so the record carries no label:
    agreement 1.0 means "no observable difference", anything less lists
    the differing inputs for a human or the verifier to judge.
    """
    keys = sorted(set(baseline) & set(candidate))
    mismatches = [k for k in keys if baseline[k] != candidate[k]]
    agreement = (len(keys) - len(mismatches)) / len(keys) if keys else 1.0
    return FeedbackRecord(
        candidate_id=candidate_id, task_id=task_id,
        kind="shadow_compare", source="telemetry", label=None,
        note=(f"影子对比：{len(keys)} 个相同输入中 {len(mismatches)} 个输出不同"
              if mismatches else f"影子对比：{len(keys)} 个相同输入输出一致"),
        round_no=round_no,
        evidence={"agreement": round(agreement, 4),
                  "mismatches": mismatches[:10],
                  "n_inputs": len(keys)})


def schedule_delayed_recheck(candidate_id: str, task_id: str,
                             adopted_ts: str) -> dict:
    """Spec for the delayed recheck: what to run, and when.

    The check itself (re-run verifier tiers A+C in a fresh worktree at the
    adopted commit) is Controller.delayed_recheck; this spec is the
    schedule a runner can pick up: 24h and 7d after adoption, bug back or
    new failure means the adoption did not hold.
    """
    return {"kind": "delayed_recheck", "candidate_id": candidate_id,
            "task_id": task_id, "adopted_ts": adopted_ts,
            "due_after_hours": [24, 168],
            "check": "re-run verifier tiers A+C in a fresh worktree at the "
                     "adopted commit; bug back or new failure = adoption "
                     "did not hold",
            "ts": _ts()}


def delayed_recheck_due(adopted_ts: str, hours: int, now: str | None = None) -> bool:
    """Has `hours` passed since adoption? Pure, testable time check."""
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        adopted = time.mktime(time.strptime(adopted_ts, fmt))
        at = time.mktime(time.strptime(now or _ts(), fmt))
    except (ValueError, TypeError):
        return False
    return (at - adopted) >= hours * 3600


# --------------------------------------------------------------------------
# Layer 2: one-tap approval card (Morde card format, no new UI).
# --------------------------------------------------------------------------

def build_approval_card(*, candidate_id: str, task_id: str,
                        behavior_change: str, tests_passed: int,
                        tests_total: int, tiers: dict | None = None,
                        cost_tokens: int | None = None,
                        cost_seconds: float | None = None,
                        score_before: float | None = None,
                        score_after: float | None = None,
                        failed_tests: list[str] | None = None,
                        needs_human_reason: str | None = None,
                        branch: str | None = None) -> dict:
    """Build one approval card. Behaviour only -- never a diff.

    `behavior_change` must describe what the user will observe
    ("routed messages now show '已分派任务' instead of '已发送'"), not how
    the code changed; diff text is rejected outright. `tiers` is the
    verifier's per-tier pass/fail ({"A": True, "B": False, ...}).
    """
    if not behavior_change or not behavior_change.strip():
        raise ValueError("behavior_change is required: what will the user observe?")
    if "diff --git" in behavior_change or "@@ " in behavior_change:
        raise ValueError("behavior_change must describe behaviour, not a diff")
    if tests_total < 0 or tests_passed < 0 or tests_passed > tests_total:
        raise ValueError("tests_passed/tests_total are inconsistent")
    return {
        "card": "rsi-approval",
        "candidate_id": candidate_id,
        "task_id": task_id,
        "branch": branch or f"rsi/{task_id}",
        "behavior_change": behavior_change.strip(),
        "tests": {"passed": tests_passed, "total": tests_total,
                  "tiers": dict(tiers or {})},
        "failed_tests": list(failed_tests or []),
        "cost": {"tokens": cost_tokens, "seconds": cost_seconds},
        "score": {"before": score_before, "after": score_after},
        "needs_human_reason": needs_human_reason,
        "actions": [dict(a) for a in CARD_ACTIONS],
        "ts": _ts(),
    }


def render_approval_card_text(card: dict) -> str:
    """One-screen text rendering of a card. Contains no diff, by construction."""
    t = card["tests"]
    tiers = ", ".join(f"{k}:{'过' if v else '没过'}"
                      for k, v in t.get("tiers", {}).items()) or "—"
    cost = card["cost"]
    cost_s = (f"{cost['tokens']} tokens" if cost.get("tokens") is not None
              else f"{cost['seconds']}s" if cost.get("seconds") is not None
              else "未计量")
    score = card["score"]
    score_s = (f"{score['before']} → {score['after']}"
               if score.get("before") is not None
               and score.get("after") is not None else "—")
    lines = [
        f"【RSI 审批】{card['task_id']} / {card['candidate_id']}",
        f"行为变化：{card['behavior_change']}",
        f"测试：{t['passed']}/{t['total']} 通过（{tiers}）",
    ]
    if card.get("failed_tests"):
        lines.append("未过测试：" + ", ".join(card["failed_tests"][:5]))
    lines.append(f"成本：{cost_s}；分数：{score_s}")
    if card.get("needs_human_reason"):
        lines.append(f"需要你定：{card['needs_human_reason']}")
    lines.append("操作：[采纳] [驳回] [修偏了]")
    return "\n".join(lines)


def batch_approval_cards(cards: list[dict], max_per_batch: int = 5) -> list[dict]:
    """Group cards for a batched weekly ask: never one card per bug per day.

    The caller groups by week; this chunks the week's cards so a human
    sees at most `max_per_batch` at once. Returns [{"batch_id", "cards"}].
    """
    if max_per_batch < 1:
        raise ValueError("max_per_batch must be >= 1")
    return [{"batch_id": f"batch-{i // max_per_batch + 1}",
             "cards": cards[i:i + max_per_batch]}
            for i in range(0, len(cards), max_per_batch)]


def needs_approval_card(*, verdict: str | None = None,
                        cost_over_budget: bool = False,
                        uncertain: bool = False) -> bool:
    """Should a human be asked at all? Success is silent by default.

    Ask only when the loop cannot decide (NEEDS_HUMAN), the cost is over
    budget, or the caller flags uncertainty (misdirected-looking fix).
    """
    return verdict == "NEEDS_HUMAN" or cost_over_budget or uncertain


def record_chat_reaction(log: FeedbackLog, *, candidate_id: str, task_id: str,
                         reaction: str,
                         round_no: int | None = None) -> dict | None:
    """Record a thumbs up/down from chat. Single-use: first tap wins.

    Returns the stored record, or None if this candidate already has a
    chat reaction (the tap "disappears" -- it is not counted twice).
    """
    mapping = {"👍": "correct", "👎": "wrong"}
    if reaction not in mapping:
        raise ValueError("reaction must be 👍 or 👎")
    if any(r.get("kind") == "chat_reaction"
           for r in log.by_candidate(candidate_id)):
        return None
    return log.append(FeedbackRecord(
        candidate_id=candidate_id, task_id=task_id, kind="chat_reaction",
        source="human", label=mapping[reaction],
        note="对话里点了 👍" if reaction == "👍" else "对话里点了 👎",
        round_no=round_no))


# --------------------------------------------------------------------------
# Layer 3: deep review -- stub only. No UI; a human opens the branch.
# --------------------------------------------------------------------------

DEEP_REVIEW_TRIGGERS = frozenset(
    {"major_feature", "large_diff", "verifier_user_conflict"})


def request_deep_review(*, candidate_id: str, task_id: str, trigger: str,
                        failed_tests: list[str] | None = None,
                        cost: dict | None = None) -> dict:
    """Spec for a deep review. STUB: no UI is implemented.

    Triggers: "major_feature" (the change is a feature, not a small fix),
    "large_diff" (change surface too big to judge from a card),
    "verifier_user_conflict" (verifier and human feedback disagree).
    The human's job is the checklist; the branch + evidence are the input.
    """
    if trigger not in DEEP_REVIEW_TRIGGERS:
        raise ValueError(f"unknown deep-review trigger: {trigger!r}")
    return {
        "status": "stub",
        "kind": "deep_review",
        "candidate_id": candidate_id,
        "task_id": task_id,
        "trigger": trigger,
        "failed_tests": list(failed_tests or []),
        "cost": dict(cost or {}),
        "checklist": [
            "打开候选分支 rsi/<task-id>，读 diff（不是只看卡片）",
            "对照 runs/<task-id> 的 reward.json，看三层证据是否齐全",
            "跑一遍受影响的测试，确认没有漏掉的行为变化",
            "决定：合入 / 改测试期望后重跑 / 驳回候选",
        ],
        "note": "No UI implemented: deep review is a human opening the "
                "branch and the evidence by hand. See docs/rrsi-mapping.md.",
    }


# --------------------------------------------------------------------------
# Adjudication: three votes, and turning feedback into learning.
# --------------------------------------------------------------------------

def three_vote_decision(*, verifier_ok: bool | None, user_label: str | None,
                        negative_streak: int, cost_ok: bool) -> dict:
    """Decide from three votes: verifier, user, cost. Two of three to adopt.

    * verifier_ok False is a VETO (repro or held-out failed -- no amount
      of user praise adopts a fix that does not fix the bug).
    * verifier_ok None is an abstention (ambiguous single-point P2P
      failure): NEEDS_HUMAN, never an auto-decision.
    * verifier_ok True still needs one more yes: a positive user label, or
      (when nobody has said anything) a cost within budget -- that is the
      silent-success path, verifier + cost = 2/3.
    * One negative label only pauses (NEEDS_HUMAN); two consecutive
      negatives reject. A positive label with cost over budget also pauses.
    """
    votes = {"verifier": verifier_ok, "user": user_label, "cost": cost_ok}
    if verifier_ok is False:
        return {"decision": "REJECT", "votes": votes,
                "reason": "verifier veto: repro or held-out failed"}
    if verifier_ok is None:
        return {"decision": "NEEDS_HUMAN", "votes": votes,
                "reason": "verifier abstained: ambiguous single-point failure"}
    if negative_streak >= 2:
        return {"decision": "REJECT", "votes": votes,
                "reason": f"two consecutive negative feedbacks (streak {negative_streak})"}
    if user_label in NEGATIVE_LABELS:
        return {"decision": "NEEDS_HUMAN", "votes": votes,
                "reason": f"single negative feedback ({user_label}); pauses auto-adopt"}
    if user_label == "correct":
        if cost_ok:
            return {"decision": "ADOPT", "votes": votes,
                    "reason": "verifier + user + cost: 3/3"}
        return {"decision": "NEEDS_HUMAN", "votes": votes,
                "reason": "user positive but cost over budget"}
    if cost_ok:
        return {"decision": "ADOPT", "votes": votes,
                "reason": "verifier + cost: 2/3, silent success (no user signal)"}
    return {"decision": "NEEDS_HUMAN", "votes": votes,
            "reason": "verifier only: cost over budget, needs human"}


def feedback_to_learning(rec: FeedbackRecord | dict, *,
                         task_id: str | None = None) -> dict:
    """Convert one feedback record into something executable -- or nothing.

    Returns {"action", "artifact", "reason"}. Actions:
    * "new_heldout_test": the failing / missed case becomes a held-out
      test SPEC (draft). A human writes the real test from the spec; the
      spec itself is never a test and must never be counted as passing.
    * "new_evolve_task": the fix was misdirected -- the spec needs
      clarifying first, as a new evolve-task spec (draft).
    * "weight_only": positive feedback (reinforces the baseline) or feedback
      too vague to convert. Recorded, never scored as a test result.
    """
    label = rec.label if isinstance(rec, FeedbackRecord) else rec.get("label")
    note = rec.note if isinstance(rec, FeedbackRecord) else rec.get("note", "")
    tid = task_id or (rec.task_id if isinstance(rec, FeedbackRecord)
                      else rec.get("task_id", "?"))
    cid = (rec.candidate_id if isinstance(rec, FeedbackRecord)
           else rec.get("candidate_id", "?"))
    if label == "correct":
        return {"action": "weight_only", "artifact": None,
                "reason": "positive feedback only reinforces the baseline"}
    if not note or len(note.strip()) < 8:
        return {"action": "weight_only", "artifact": None,
                "reason": "feedback too vague to convert into a test or task"}
    if label in ("wrong", "missed_point", "introduced_bug"):
        return {"action": "new_heldout_test",
                "artifact": {"kind": "heldout_test_spec", "draft": True,
                             "suggested_dir": f"tasks/heldout/{tid}",
                             "title": note.strip(),
                             "source_candidate": cid,
                             "note": "Human writes the real test from this "
                                     "spec. The spec alone is not a test."},
                "reason": f"{label}: the failing/missed case becomes a held-out case"}
    if label == "misdirected":
        return {"action": "new_evolve_task",
                "artifact": {"kind": "evolve_task_spec", "draft": True,
                             "title": f"clarify spec: {note.strip()}",
                             "source_candidate": cid,
                             "note": "Clarify what the fix should have done "
                                     "before re-running the fixer."},
                "reason": "misdirected: the spec, not the code, needs fixing first"}
    return {"action": "weight_only", "artifact": None,
            "reason": "no label to convert from (neutral signal)"}


# --------------------------------------------------------------------------
# Leakage: what the Fixer may see. Prior rounds only, sanitised, and never
# any held-out content.
# --------------------------------------------------------------------------

def sanitize_for_fixer(records: list[dict],
                       heldout_markers: tuple[str, ...] = ()) -> list[dict]:
    """Reduce feedback records to what a Fixer brief may contain.

    Only label + one-line note survive; evidence is dropped entirely, and
    any record whose note mentions a held-out marker is dropped whole.
    """
    out = []
    for r in records:
        note = r.get("note", "") if isinstance(r, dict) else r.note
        if any(m and m in note for m in heldout_markers):
            continue
        label = r.get("label") if isinstance(r, dict) else r.label
        out.append({"label": label, "note": note})
    return out


def feedback_for_fixer(log: FeedbackLog, task_id: str, current_round: int,
                       heldout_markers: tuple[str, ...] = ()) -> list[dict]:
    """Sanitised prior-round feedback for one task.

    Only records from rounds strictly before `current_round` are visible;
    records with no round number are excluded (their round cannot be
    proven). Held-out markers are filtered by sanitize_for_fixer.
    """
    prior = [r for r in log.by_task(task_id)
             if isinstance(r.get("round_no"), int)
             and r["round_no"] < current_round]
    return sanitize_for_fixer(prior, heldout_markers)
