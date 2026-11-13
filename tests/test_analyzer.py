"""Tests for rsi/analyzer.py, on hand-written ledgers.

Everything here is synthetic and labelled as such: a few ADOPT / REJECT /
NEEDS_HUMAN records, a few telemetry records, and reward dicts with
hand-written tier B per-test maps. No real run is involved.
"""
from rsi.analyzer import (analyze, analyze_files, cluster_to_draft,
                          component_win_rate, failure_clusters, flaky_tests,
                          load_jsonl, report_markdown, task_type,
                          write_report)


def _rec(task_id, verdict, component=None, round_no=0, candidate="c1"):
    return {"round": round_no, "task_id": task_id,
            "candidate_id": f"{task_id}-{candidate}",
            "component": component, "verdict": verdict, "score": 0.8,
            "fixer": "manual-pilot", "ts": "2026-11-13 01:00:00"}


HISTORY = [
    _rec("FIX-001", "ADOPT", "logic-small", 0),
    _rec("FIX-002", "ADOPT", "logic-small", 1),
    _rec("FIX-003", "REJECT", "logic-small", 2),
    _rec("CAND-001", "REJECT", "ui-text", 0),
    _rec("CAND-001", "REJECT", "ui-text", 1),
    _rec("CAND-001", "REJECT", "ui-text", 2),
    _rec("SELF-001", "NEEDS_HUMAN", "config-default", 0),
    _rec("SELF-001", "NEEDS_HUMAN", "config-default", 1),
    _rec("MORDE-009", "STUCK", None, 0),
]

TELEMETRY = [
    {"candidate_id": "CAND-001-c1", "task_id": "CAND-001",
     "kind": "manual_revert", "source": "telemetry", "label": "wrong",
     "note": "用户手动改回了 RSI 改过的文件：x.ts"},
    {"candidate_id": "CAND-001-c1", "task_id": "CAND-001",
     "kind": "manual_revert", "source": "telemetry", "label": "wrong",
     "note": "用户又改回了一次"},
    {"candidate_id": "FIX-001-c1", "task_id": "FIX-001",
     "kind": "approval_approve", "source": "telemetry", "label": "correct",
     "note": "审批卡通过"},
    {"candidate_id": "FIX-002-c1", "task_id": "FIX-002",
     "kind": "repeat_intent", "source": "telemetry", "label": "missed_point",
     "note": "用户重复提出同一意图：修一下文案"},
    {"candidate_id": "FIX-002-c1", "task_id": "FIX-002",
     "kind": "shadow_compare", "source": "telemetry", "label": None,
     "note": "影子对比：3 个相同输入输出一致"},
]


def _reward(task_id, round_no, tests_b):
    return {"task_id": task_id, "round": round_no, "passed": True,
            "score": 1.0,
            "tiers": {"B": {"passed": True, "status": "run",
                            "checks": [{"name": "B-0", "passed": True,
                                        "tests": dict(tests_b)}]}}}


REWARDS = [
    _reward("FIX-001", 0, {"test_a": True, "test_b": False, "test_c": True}),
    _reward("FIX-001", 1, {"test_a": False, "test_b": False, "test_c": True}),
    _reward("FIX-001", 2, {"test_a": True, "test_b": True, "test_c": True}),
]


def test_failure_clusters_count_by_task_component_verdict():
    clusters = failure_clusters(HISTORY)
    by_key = {(c["task_id"], c["component"], c["verdict"]): c["n"]
              for c in clusters}
    assert by_key[("CAND-001", "ui-text", "REJECT")] == 3
    assert by_key[("SELF-001", "config-default", "NEEDS_HUMAN")] == 2
    assert by_key[("FIX-001", "logic-small", "ADOPT")] == 1
    assert by_key[("MORDE-009", "未标注", "STUCK")] == 1
    assert clusters[0]["task_id"] == "CAND-001"  # most frequent first


def test_component_win_rate_and_unlabelled_fallback():
    cwr = component_win_rate(HISTORY)
    by_comp = {r["group"]: r for r in cwr["by_component"]}
    assert by_comp["logic-small"] == {"group": "logic-small", "n": 3,
                                      "adopt": 2, "adopt_rate": 0.6667}
    assert by_comp["ui-text"]["adopt_rate"] == 0.0
    assert cwr["unlabelled"] == 1
    by_type = {r["group"]: r for r in cwr["by_task_type"]}
    assert by_type["MORDE"]["n"] == 1  # the unlabelled record, by task type
    assert by_type["CAND"]["n"] == 3


def test_task_type_prefix():
    assert task_type("CAND-001") == "CAND"
    assert task_type("SELF-0001") == "SELF"
    assert task_type("") == "?"


def test_flaky_tests_detect_flips():
    fl = flaky_tests(REWARDS)
    assert fl["available"]
    by_test = {t["test"]: t for t in fl["tests"]}
    assert by_test["test_a"]["flips"] == 2  # pass -> fail -> pass
    assert by_test["test_a"]["sequence"] == ["pass", "fail", "pass"]
    assert by_test["test_b"]["flips"] == 1  # fail -> fail -> pass
    assert "test_c" not in by_test  # never changed: not flaky
    assert fl["tests"][0]["test"] == "test_a"  # most flips first


def test_flaky_tests_no_per_test_data():
    fl = flaky_tests([{"task_id": "X", "tiers": {"B": {"passed": True,
                                                       "status": "run",
                                                       "checks": [{"name": "B-0",
                                                                     "passed": True}]}}}])
    assert not fl["available"] and fl["tests"] == []
    assert flaky_tests(None)["available"] is False
    assert flaky_tests([])["available"] is False


def test_cluster_to_draft_repeated_reject_is_evolve_task():
    d = cluster_to_draft({"task_id": "CAND-001", "component": "ui-text",
                          "verdict": "REJECT", "n": 3})
    assert d["action"] == "new_evolve_task"
    assert d["artifact"]["draft"] is True
    assert d["artifact"]["kind"] == "evolve_task_spec"
    assert d["artifact"]["source"] == "analyzer"
    assert d["artifact"]["source_task"] == "CAND-001"


def test_cluster_to_draft_repeated_needs_human_is_heldout():
    d = cluster_to_draft({"task_id": "SELF-001", "component": "config-default",
                          "verdict": "NEEDS_HUMAN", "n": 2})
    assert d["action"] == "new_heldout_test"
    assert d["artifact"]["kind"] == "heldout_test_spec"
    assert d["artifact"]["suggested_dir"] == "tasks/heldout/SELF-001"


def test_cluster_to_draft_singleton_and_stuck_have_no_draft():
    assert cluster_to_draft({"task_id": "X", "component": "ui-text",
                             "verdict": "REJECT", "n": 1}) is None
    assert cluster_to_draft({"task_id": "X", "component": None,
                             "verdict": "STUCK", "n": 3}) is None
    assert cluster_to_draft({"task_id": "X", "component": None,
                             "verdict": "GAMING-DETECTED", "n": 2}) is None


def test_analyze_full_report_and_drafts():
    rep = analyze(HISTORY, TELEMETRY, REWARDS)
    assert rep["n_history"] == 9 and rep["n_telemetry"] == 5
    assert rep["verdict_counts"] == {"REJECT": 4, "ADOPT": 2,
                                     "NEEDS_HUMAN": 2, "STUCK": 1}
    assert rep["telemetry"]["by_kind"] == {"manual_revert": 2,
                                           "approval_approve": 1,
                                           "repeat_intent": 1,
                                           "shadow_compare": 1}
    assert rep["telemetry"]["by_polarity"] == {"negative": 3, "positive": 1,
                                               "neutral": 1}
    assert len(rep["drafts"]) == 2  # CAND-001 evolve + SELF-001 heldout
    assert {d["action"] for d in rep["drafts"]} == {"new_evolve_task",
                                                   "new_heldout_test"}
    assert any("MORDE-009" in w and "卡住" in w for w in rep["warnings"])


def test_analyze_empty_inputs_do_not_crash():
    rep = analyze([], [], None)
    assert rep["n_history"] == 0 and rep["verdict_counts"] == {}
    assert rep["failure_clusters"] == [] and rep["drafts"] == []
    assert rep["flaky_tests"]["available"] is False
    assert "无记录" in report_markdown(rep)


def test_report_markdown_contains_sections_and_numbers():
    md = report_markdown(analyze(HISTORY, TELEMETRY, REWARDS))
    for section in ("裁决总览", "失败模式聚类", "组件胜率", "抖动测试",
                    "遥测信号", "草案"):
        assert section in md
    assert "CAND-001" in md and "test_a" in md
    assert "66.67%" in md  # logic-small adopt rate
    assert "没有组件标签" in md  # the unlabelled MORDE-009 record is noted
    assert "不自动写进 evolve.json" in md


def test_analyze_files_and_write_report_roundtrip(tmp_path):
    hist = tmp_path / "history.jsonl"
    tele = tmp_path / "telemetry.jsonl"
    hist.write_text("\n".join(__import__("json").dumps(r, ensure_ascii=False)
                              for r in HISTORY[:3]) + "\n")
    tele.write_text("")
    rep = analyze_files(hist, tele)
    assert rep["n_history"] == 3 and rep["n_telemetry"] == 0
    assert load_jsonl(tmp_path / "missing.jsonl") == []
    out = write_report(rep, tmp_path / "sub" / "report.md")
    assert out.exists() and "RSI 分析报告" in out.read_text()
