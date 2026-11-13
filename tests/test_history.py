from rsi.history import History


def test_append_and_records(tmp_path):
    h = History(tmp_path / "history.jsonl")
    assert h.records() == []
    h.append_candidate(0, "FIX-001", "FIX-001-c1", "A", "logic-small",
                       "remove +1", "diff text", {"A": {"passed": True}},
                       "ADOPT", "abc123", 1.0, None, 12.5, fixer="manual-pilot")
    recs = h.records()
    assert len(recs) == 1
    r = recs[0]
    assert r["task_id"] == "FIX-001" and r["verdict"] == "ADOPT"
    assert r["score"] == 1.0 and r["fixer"] == "manual-pilot"
    assert r["diff_sha256"] and len(r["diff_sha256"]) == 64
    assert "ts" in r


def test_frontier_tracks_last_adopt(tmp_path):
    h = History(tmp_path / "history.jsonl")
    assert h.frontier() == {}
    h.append_candidate(0, "FIX-001", "c1", "A", None, None, "d1", {},
                       "REJECT", "aaa", 0.5, None, 1.0)
    assert h.frontier() == {}
    h.append_candidate(1, "FIX-001", "c2", "A", None, None, "d2", {},
                       "ADOPT", "bbb", 1.0, None, 1.0)
    fr = h.frontier()
    assert fr["baseline_commit"] == "bbb" and fr["n_accepted"] == 1
    assert h.attempted_tasks() == {"FIX-001"}
    assert len(h.accepted()) == 1

# ------------------------------------------------------- stall / explore --
from rsi.components import K
from rsi.history import exploration, stall_flag


def _rec(h, round_no, task_id, component, verdict, score):
    h.append_candidate(round_no, task_id, "c1", "A", component, None, "d",
                       {}, verdict, "aaa", score, None, 1.0)


def test_stall_flag_boundaries():
    # Too few rounds: 0, however flat the scores.
    assert stall_flag([0.5, 0.5], 1, 3, 0.02) == 0
    assert stall_flag([], 0, 3, 0.02) == 0
    # t beyond the trajectory: 0.
    assert stall_flag([0.5, 0.5, 0.5, 0.5], 4, 3, 0.02) == 0
    # Gain above delta: not stalled.
    assert stall_flag([0.50, 0.60, 0.70, 0.72], 3, 3, 0.02) == 0
    # Gain inside delta: stalled.
    assert stall_flag([0.70, 0.71, 0.71, 0.71], 3, 3, 0.02) == 1
    # Exactly on delta counts as stalled (<=). Binary-exact values only:
    # 0.75 - 0.50 == 0.25 exactly, unlike 0.52 - 0.50 in float.
    assert stall_flag([0.50, 0.50, 0.50, 0.75], 3, 3, 0.25) == 1
    # w = 1: compare with the previous round only.
    assert stall_flag([0.5, 0.51], 1, 1, 0.02) == 1
    assert stall_flag([0.5, 0.60], 1, 1, 0.02) == 0


def test_tried_counts_measured_only(tmp_path):
    h = History(tmp_path / "history.jsonl")
    _rec(h, 0, "T1", "logic-small", "ADOPT", 0.8)
    _rec(h, 1, "T2", "portal-ui", "REJECT", 0.5)
    _rec(h, 2, "T3", "ui-text", "NEEDS_HUMAN", 0.7)
    _rec(h, 3, "T4", "docs", "SKIPPED", 0.9)   # no fix attempted
    h.append_candidate(4, "T5", "c1", "A", "config-default", None, "d", {},
                       "STUCK", "aaa", None, None, 1.0)  # no measurement
    assert h.tried() == {"logic-small", "portal-ui", "ui-text"}


def test_exploration_three_cases():
    stalled = exploration(4, 1, {"logic-small"}, 1)
    assert stalled["sigma"] == 1
    assert "logic-small" not in stalled["untried"]
    assert "portal-ui" in stalled["untried"]
    assert "停滞" in stalled["text"] and "保留" in stalled["text"]
    calm = exploration(4, 0, {"logic-small"}, 1)
    assert calm["sigma"] == 0
    assert "不强制" in calm["text"]
    done = exploration(4, 0, set(K), 1)
    assert done["untried"] == []
    assert "至少试过一次" in done["text"]


def test_trajectory_tracks_incumbent_not_candidate(tmp_path):
    h = History(tmp_path / "history.jsonl")
    _rec(h, 0, "T1", "logic-small", "REJECT", 0.5)
    _rec(h, 1, "T2", "logic-small", "ADOPT", 0.8)
    _rec(h, 2, "T3", "portal-ui", "REJECT", 0.9)  # high candidate score...
    assert h.trajectory() == [0.0, 0.8, 0.8]     # ...but incumbent stays


def test_stuck_components_needs_two_consecutive_rejects(tmp_path):
    h = History(tmp_path / "history.jsonl")
    _rec(h, 0, "T1", "logic-small", "REJECT", 0.5)
    assert h.stuck_components() == set()
    _rec(h, 1, "T2", "logic-small", "REJECT", 0.5)
    assert h.stuck_components() == {"logic-small"}
    # An ADOPT resets the run, even for the same component.
    _rec(h, 2, "T3", "logic-small", "ADOPT", 0.8)
    assert h.stuck_components() == set()
    # One more REJECT alone is not stuck; a NEEDS_HUMAN in between does
    # not reset the decided run either (it is not a decided outcome).
    _rec(h, 3, "T4", "logic-small", "REJECT", 0.5)
    _rec(h, 4, "T5", "logic-small", "NEEDS_HUMAN", 0.6)
    assert h.stuck_components() == set()
    _rec(h, 5, "T6", "logic-small", "REJECT", 0.5)
    assert h.stuck_components() == {"logic-small"}


def test_prune_set_morde_rule(tmp_path):
    h = History(tmp_path / "history.jsonl")
    # Round 0 is outside the window for t=5, n_prune=4 ([1, 5)).
    _rec(h, 0, "T0a", "docs", "REJECT", 0.5)
    _rec(h, 0, "T0b", "docs", "REJECT", 0.5)
    # logic-small: 3 rejects, 0 adopts inside the window -> pruned.
    _rec(h, 1, "T1", "logic-small", "REJECT", 0.5)
    _rec(h, 2, "T2", "logic-small", "REJECT", 0.5)
    _rec(h, 3, "T3", "logic-small", "REJECT", 0.5)
    # portal-ui and docs: 2 rejects each, 0 adopts -> pruned as well.
    _rec(h, 2, "T4", "portal-ui", "REJECT", 0.5)
    _rec(h, 4, "T7", "portal-ui", "REJECT", 0.5)
    _rec(h, 1, "T8", "docs", "REJECT", 0.5)
    _rec(h, 4, "T9", "docs", "REJECT", 0.5)
    # ui-text: adopted once -> has yield, kept.
    _rec(h, 4, "T6", "ui-text", "ADOPT", 0.8)
    assert h.prune_set(5, 4) == [
        {"component": "logic-small", "attempts": 3,
         "adopted": 0, "window_rounds": 4},
        {"component": "docs", "attempts": 2,
         "adopted": 0, "window_rounds": 4},
        {"component": "portal-ui", "attempts": 2,
         "adopted": 0, "window_rounds": 4}]
    # The cap is honoured: with n_prune=2 the window is rounds [3, 5)
    # and three components qualify there with 2 attempts each, but only
    # the first two (attempts desc, then name) are returned.
    h2 = History(tmp_path / "history2.jsonl")
    for rnd in (3, 4):
        for i, comp in enumerate(("docs", "logic-small", "portal-ui")):
            _rec(h2, rnd, f"C{rnd}-{i}", comp, "REJECT", 0.5)
    pruned = h2.prune_set(5, 2)
    assert [p["component"] for p in pruned] == ["docs", "logic-small"]
    # Round 0: no history yet, nothing to prune.
    assert History(tmp_path / "empty.jsonl").prune_set(0, 4) == []
