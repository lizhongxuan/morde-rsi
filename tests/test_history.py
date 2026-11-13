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
