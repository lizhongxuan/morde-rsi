"""Tests for rsi/feedback.py (layers 1-3) and the leakage guards.

No Morde repo, no network: telemetry detectors are pure functions over
plain data, the approval card is a data structure, and the three-vote /
NEEDS_HUMAN rules are pure decisions. Controller end-to-end coverage of
the NEEDS_HUMAN route lives in tests/test_controller.py.
"""
import pytest

from rsi import feedback as fb
from rsi.critic import review, review_brief
from rsi.feedback import (FeedbackLog, FeedbackRecord, approval_record,
                          batch_approval_cards, build_approval_card,
                          delayed_recheck_due, detect_manual_revert,
                          detect_repeat_intent, feedback_for_fixer,
                          feedback_to_learning, needs_approval_card,
                          record_chat_reaction, render_approval_card_text,
                          request_deep_review, sanitize_for_fixer,
                          schedule_delayed_recheck, shadow_compare,
                          three_vote_decision)
from rsi.selection import classify_p2p_failure


def _rec(**kw):
    base = {"candidate_id": "C1", "task_id": "T1", "kind": "human_label",
            "note": "修完后状态显示对了", "label": "correct"}
    base.update(kw)
    return FeedbackRecord(**base)


# ---------------------------------------------------------------- records --
def test_record_validation_ok_and_bad():
    assert _rec().validate() == []
    assert "unknown label" in " ".join(_rec(label="great").validate())
    assert "unknown kind" in " ".join(_rec(kind="vibes").validate())
    assert "note is required" in " ".join(_rec(note="   ").validate())
    assert "too vague" not in " ".join(_rec(note="x" * 501).validate())
    assert "longer than" in " ".join(_rec(note="x" * 501).validate())
    assert "verifier material" in " ".join(
        _rec(evidence={"heldout_content": "x"}).validate())
    assert "candidate_id" in " ".join(_rec(candidate_id="").validate())


def test_log_append_rejects_invalid(tmp_path):
    log = FeedbackLog(tmp_path / "human_feedback.jsonl")
    with pytest.raises(ValueError):
        log.append(_rec(label="nope"))
    assert log.records() == []
    stored = log.append(_rec())
    assert stored["ts"] and stored["candidate_id"] == "C1"
    assert len(log.records()) == 1
    assert log.by_candidate("C1") == log.by_task("T1") == log.records()


def test_negative_streak_single_pauses_double_kills(tmp_path):
    log = FeedbackLog(tmp_path / "human_feedback.jsonl")
    assert log.negative_streak("C1") == 0
    log.append(_rec(kind="chat_reaction", label="wrong", note="点了 👎"))
    assert log.negative_streak("C1") == 1
    log.append(_rec(kind="chat_reaction", label="wrong", note="又点了 👎"))
    assert log.negative_streak("C1") == 2
    log.append(_rec(kind="chat_reaction", label="correct", note="点了 👍"))
    assert log.negative_streak("C1") == 0  # positive resets
    # Neutral (shadow compare, no label) neither extends nor resets.
    log.append(_rec(kind="shadow_compare", label=None, note="影子对比：一致"))
    assert log.negative_streak("C1") == 0


def test_has_resolution_is_round_scoped(tmp_path):
    log = FeedbackLog(tmp_path / "human_feedback.jsonl")
    log.append(_rec(kind="human_resolution", label=None, round_no=0,
                    note="update-test-expectation: 改断言"))
    assert log.has_resolution("C1")
    assert log.has_resolution("C1", after_round=0)
    assert not log.has_resolution("C1", after_round=1)  # later round: stale


# ------------------------------------------------------------- telemetry --
def test_repeat_intent_detects_same_intent_only():
    rec = detect_repeat_intent(["帮我修这个 bug"], "帮我修这个bug!",
                               candidate_id="C1", task_id="T1")
    assert rec is not None and rec.label == "missed_point"
    assert rec.kind == "repeat_intent" and rec.source == "telemetry"
    assert detect_repeat_intent(["帮我修这个 bug"], "换个话题",
                                candidate_id="C1", task_id="T1") is None
    assert detect_repeat_intent([], "帮我修这个 bug",
                                candidate_id="C1", task_id="T1") is None


def test_manual_revert_needs_overlap():
    rec = detect_manual_revert(["a.py", "b.py"], ["b.py", "c.py"],
                               candidate_id="C1", task_id="T1")
    assert rec is not None and rec.label == "wrong"
    assert rec.evidence["reverted_files"] == ["b.py"]
    assert detect_manual_revert(["a.py"], ["c.py"],
                                candidate_id="C1", task_id="T1") is None


def test_approval_record_outcomes():
    assert approval_record("rejected", candidate_id="C1",
                           task_id="T1").label == "wrong"
    assert approval_record("approved", candidate_id="C1",
                           task_id="T1").label == "correct"
    assert approval_record("pending", candidate_id="C1",
                           task_id="T1") is None


def test_shadow_compare_is_evidence_not_verdict():
    rec = shadow_compare({"in1": "x", "in2": "y"}, {"in1": "x", "in2": "y"},
                         candidate_id="C1", task_id="T1")
    assert rec.label is None and rec.evidence["agreement"] == 1.0
    rec = shadow_compare({"in1": "x", "in2": "y"}, {"in1": "x", "in2": "z"},
                         candidate_id="C1", task_id="T1")
    assert rec.evidence["agreement"] == 0.5
    assert rec.evidence["mismatches"] == ["in2"]


def test_delayed_recheck_schedule_and_due():
    spec = schedule_delayed_recheck("C1", "T1", "2026-11-13 00:00:00")
    assert spec["due_after_hours"] == [24, 168]
    assert delayed_recheck_due("2026-11-13 00:00:00", 24,
                               now="2026-11-14 00:00:01") is True
    assert delayed_recheck_due("2026-11-13 00:00:00", 24,
                               now="2026-11-13 23:00:00") is False
    assert delayed_recheck_due("not-a-date", 24) is False


# ---------------------------------------------------------- approval card --
def _card(**kw):
    base = {"candidate_id": "C1", "task_id": "T1",
            "behavior_change": "已分派的消息显示“已分派任务”",
            "tests_passed": 9, "tests_total": 10}
    base.update(kw)
    return build_approval_card(**base)


def test_approval_card_shape_and_render():
    card = _card(tiers={"A": True, "B": False, "C": True},
                 failed_tests=["Projects.test.tsx:355"],
                 cost_seconds=12.5, score_before=0.6, score_after=0.8,
                 needs_human_reason="单点失败，可能是测试写死了 bug")
    assert [a["id"] for a in card["actions"]] == ["adopt", "reject",
                                                  "misdirected"]
    assert card["tests"] == {"passed": 9, "total": 10,
                             "tiers": {"A": True, "B": False, "C": True}}
    text = render_approval_card_text(card)
    assert "已分派的消息显示" in text and "9/10" in text
    assert "0.6 → 0.8" in text and "Projects.test.tsx:355" in text
    assert "diff --git" not in text and "@@ " not in text


def test_approval_card_rejects_diff_and_bad_counts():
    with pytest.raises(ValueError):
        _card(behavior_change="diff --git a/x b/x\n@@ -1 +1 @@")
    with pytest.raises(ValueError):
        _card(behavior_change="  ")
    with pytest.raises(ValueError):
        _card(tests_passed=11, tests_total=10)


def test_batch_cards_chunks_weekly_ask():
    cards = [_card(candidate_id=f"C{i}") for i in range(7)]
    batches = batch_approval_cards(cards, max_per_batch=5)
    assert [len(b["cards"]) for b in batches] == [5, 2]
    assert batch_approval_cards([]) == []
    with pytest.raises(ValueError):
        batch_approval_cards(cards, max_per_batch=0)


def test_needs_approval_card_silent_by_default():
    assert needs_approval_card() is False
    assert needs_approval_card(verdict="NEEDS_HUMAN") is True
    assert needs_approval_card(cost_over_budget=True) is True
    assert needs_approval_card(uncertain=True) is True
    assert needs_approval_card(verdict="ADOPT") is False


def test_chat_reaction_single_use(tmp_path):
    log = FeedbackLog(tmp_path / "human_feedback.jsonl")
    first = record_chat_reaction(log, candidate_id="C1", task_id="T1",
                                 reaction="👍")
    assert first is not None and first["label"] == "correct"
    assert record_chat_reaction(log, candidate_id="C1", task_id="T1",
                                reaction="👎") is None  # already tapped
    assert len(log.records()) == 1
    with pytest.raises(ValueError):
        record_chat_reaction(log, candidate_id="C2", task_id="T1",
                             reaction="meh")


def test_deep_review_is_stub_only():
    spec = request_deep_review(candidate_id="C1", task_id="T1",
                               trigger="verifier_user_conflict")
    assert spec["status"] == "stub" and len(spec["checklist"]) >= 3
    with pytest.raises(ValueError):
        request_deep_review(candidate_id="C1", task_id="T1",
                            trigger="just_curious")


# ------------------------------------------------------------- adjudication --
@pytest.mark.parametrize("verifier_ok,user_label,streak,cost_ok,expected", [
    (False, "correct", 0, True, "REJECT"),       # verifier veto
    (False, None, 0, True, "REJECT"),
    (None, None, 0, True, "NEEDS_HUMAN"),        # verifier abstains
    (True, "correct", 0, True, "ADOPT"),         # 3/3
    (True, "correct", 0, False, "NEEDS_HUMAN"),  # cost over budget
    (True, None, 0, True, "ADOPT"),              # silent success: 2/3
    (True, None, 0, False, "NEEDS_HUMAN"),
    (True, "wrong", 1, True, "NEEDS_HUMAN"),     # single negative pauses
    (True, "wrong", 2, True, "REJECT"),          # two negatives kill
    (True, None, 2, True, "REJECT"),
])
def test_three_vote_decision_matrix(verifier_ok, user_label, streak, cost_ok,
                                    expected):
    d = three_vote_decision(verifier_ok=verifier_ok, user_label=user_label,
                            negative_streak=streak, cost_ok=cost_ok)
    assert d["decision"] == expected, d


def test_feedback_to_learning_conversions():
    wrong = feedback_to_learning(_rec(label="wrong", note="修完还是显示已发送"))
    assert wrong["action"] == "new_heldout_test"
    assert wrong["artifact"]["draft"] is True
    assert "T1" in wrong["artifact"]["suggested_dir"]
    assert "Human writes the real test" in wrong["artifact"]["note"]
    missed = feedback_to_learning(_rec(label="missed_point",
                                       note="路由状态还是没出来"))
    assert missed["action"] == "new_heldout_test"
    mis = feedback_to_learning(_rec(label="misdirected",
                                    note="我要的是改文案不是改路由"))
    assert mis["action"] == "new_evolve_task"
    assert mis["artifact"]["draft"] is True
    assert feedback_to_learning(_rec(label="correct"))["action"] == "weight_only"
    assert feedback_to_learning(
        _rec(label="wrong", note="不行"))["action"] == "weight_only"
    assert feedback_to_learning(
        _rec(label=None, note="影子对比发现输出不同"))["action"] == "weight_only"


# ------------------------------------------------------------------ leakage --
def test_sanitize_and_feedback_for_fixer(tmp_path):
    log = FeedbackLog(tmp_path / "human_feedback.jsonl")
    log.append(_rec(kind="human_label", label="wrong", round_no=0,
                    note="第一轮：修错了方向", evidence={"secret": "x"}))
    log.append(_rec(kind="human_label", label="wrong", round_no=1,
                    note="第二轮：还是不对"))
    log.append(_rec(kind="human_label", label="wrong", round_no=None,
                    note="没标轮次的反馈"))
    log.append(_rec(kind="human_label", label="wrong", round_no=0,
                    note="提到了 message-status-heldout.test.ts 的反馈"))
    visible = feedback_for_fixer(
        log, "T1", 1, heldout_markers=("message-status-heldout.test.ts",))
    assert visible == [{"label": "wrong", "note": "第一轮：修错了方向"}]
    # Round 1's own feedback is not visible in round 1; evidence is gone.
    assert all(set(v) == {"label", "note"} for v in visible)


def test_critic_blocks_feedback_files_and_brief_leaks():
    diff = ("diff --git a/human_feedback.jsonl b/human_feedback.jsonl\n"
            "--- a/human_feedback.jsonl\n+++ b/human_feedback.jsonl\n+{}\n")
    assert review(diff)["gaming"]
    diff2 = ("diff --git a/rsi/feedback.py b/rsi/feedback.py\n"
             "--- a/rsi/feedback.py\n+++ b/rsi/feedback.py\n+pass\n")
    assert review(diff2)["gaming"]
    # A brief naming a held-out file is rejected; a clean brief is fine --
    # even though the rules text itself mentions reward.json (SKILL.md).
    assert review_brief("fix the bug; see message-status-heldout.test.ts",
                        heldout_markers=["message-status-heldout.test.ts"]
                        )["verdict"] == "reject"
    assert review_brief("do not touch tests, verifier, reward.json",
                        heldout_markers=["message-status-heldout.test.ts"]
                        )["verdict"] == "accept"


def test_render_brief_carries_prior_feedback_only():
    from rsi.fixer import DSHFixer, render_brief
    brief = render_brief({"task_id": "T1", "description": "bug",
                          "repro": "pytest"},
                         "/tmp/wt", "rules",
                         prior_feedback=[{"label": "wrong",
                                          "note": "上一轮修偏了"}])
    assert "上一轮修偏了" in brief and "held-out" not in brief
    # The stub still refuses to run, but the brief it shows carries feedback.
    fixer = DSHFixer()
    with pytest.raises(NotImplementedError) as e:
        fixer.fix({"task_id": "T1", "description": "bug", "repro": "pytest",
                   "prior_feedback": [{"label": "wrong", "note": "上一轮修偏了"}]},
                  "/tmp/wt", {})
    assert "上一轮修偏了" in str(e.value)
    # A brief that would leak a held-out marker is a wiring bug, not a fix.
    with pytest.raises(RuntimeError) as e2:
        fixer.fix({"task_id": "T1", "description": "bug",
                   "repro": "see secret-heldout.test.ts",
                   "heldout_markers": ["secret-heldout.test.ts"]},
                  "/tmp/wt", {})
    assert "leaked" in str(e2.value)


# ------------------------------------------------------- p2p classification --
def test_classify_p2p_ambiguous_single():
    c = classify_p2p_failure({"A": True, "B": True}, {"A": False, "B": True},
                             tier_a_pass=True, tier_c_pass=True)
    assert c["kind"] == "ambiguous_single" and c["failed"] == ["A"]


def test_classify_p2p_clear_cases():
    base = {"A": True, "B": True, "C": True}
    cand = {"A": False, "B": False, "C": True}
    assert classify_p2p_failure(base, cand, tier_a_pass=True,
                                tier_c_pass=True)["kind"] == "clear_regression"
    assert classify_p2p_failure({"A": True}, {"A": False}, tier_a_pass=False,
                                tier_c_pass=True)["kind"] == "clear_regression"
    assert classify_p2p_failure(base, {"A": True, "B": True, "C": True},
                                tier_a_pass=True,
                                tier_c_pass=True)["kind"] == "clean"


def test_classify_p2p_relatedness_narrows():
    base, cand = {"A": True}, {"A": False}
    assert classify_p2p_failure(
        base, cand, tier_a_pass=True, tier_c_pass=True,
        related_tests={"A"})["kind"] == "ambiguous_single"
    assert classify_p2p_failure(
        base, cand, tier_a_pass=True, tier_c_pass=True,
        related_tests={"SomethingElse"})["kind"] == "clear_regression"
