"""Deterministic analyzer: turn the ledger into a small report. No LLM.

Reads history.jsonl (one record per candidate) + telemetry.jsonl (layer 1
feedback) and, when per-test data is available, the reward dicts the
verifier wrote (runs/<domain>/jobs/*/reward.json). Pure stdlib, pure
functions over plain data: the same record lists can come from files or
from hand-written fixtures in tests.

What it produces (see analyze()):
* failure-mode clusters: (task_id, component, verdict) counts;
* component win rates: ADOPT rate per component label. Records without a
  component label are grouped by task-type instead (task_id prefix, e.g.
  CAND / SELF / MORDE) and the report says so -- the analyzer never
  invents a component;
* flaky tests: tests whose pass/fail flips across rounds, from tier B
  per-test maps (go-test-json tiers). Plain-command tiers (e.g. the self
  domain's `pytest -q`) carry no per-test map, so the section then
  reports "no per-test data" instead of guessing from logs;
* telemetry summary: counts by kind and by polarity;
* drafts: failure clusters with >= 2 records become evolve-task /
  held-out-test DRAFTS, built by reusing feedback.feedback_to_learning
  (synthetic record + the same artifact shape). Drafts are never written
  into evolve.json and never scored; a human decides whether to adopt one.

This replaces RRSI's analyst.py/digester.py for the pilot: RRSI's analyzer
is an LLM pass over the ledger; morde-rsi is local-only, so this is the
deterministic version, and it deliberately cannot see held-out content --
it only reads verdicts, counts and test names.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import feedback as fb
from . import selection as sel

# A cluster needs this many records with the same (task, component,
# verdict) before the analyzer drafts anything from it: one failure is
# signal, two is a pattern.
MIN_CLUSTER_FOR_DRAFT = 2

#: How a repeated failure cluster maps to a draft, via a synthetic
#: feedback label fed to feedback.feedback_to_learning (the label is the
#: analyzer's interpretation of the cluster, not a human label -- the
#: draft artifact is marked source="analyzer" so it can never be mistaken
#: for human feedback).
CLUSTER_LABELS = {
    # Same task failing the same way twice: the spec/task shape is the
    # problem, not a missing test case (tier A already pins the case) --
    # clarify or split the task first.
    "REJECT": "misdirected",
    # Same task stuck at NEEDS_HUMAN twice: the disputed behaviour needs
    # a pinned held-out case before anyone re-runs the fixer.
    "NEEDS_HUMAN": "wrong",
}
#: Verdicts that never produce a draft: STUCK is a fixer/infra problem
#: (fix the wiring, not the task); GAMING-DETECTED is a guardrail hit
#: (a human reviews the attempt, it does not become a task).
NO_DRAFT_VERDICTS = frozenset({"STUCK", "GAMING-DETECTED", "SKIPPED"})


def load_jsonl(path: Path | str) -> list[dict]:
    """Read a JSONL ledger; missing file -> []."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def load_rewards(runs_dir: Path | str) -> list[dict]:
    """All reward.json files under a runs dir, sorted by path.

    Rewards carry per-test maps only for go-test-json tiers; plain
    commands (e.g. the self domain's `pytest -q`) have none, and the
    flaky-test section then reports that honestly. NOTE: the controller
    does not currently persist reward.json for real rounds (it keeps
    only the tier summary in history.jsonl) -- see analyze() docs.
    """
    out = []
    for p in sorted(Path(runs_dir).rglob("reward.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def task_type(task_id: str) -> str:
    """Rough task type from the id prefix (CAND-001 -> CAND, SELF-0001 ->
    SELF). Used only as the fallback grouping when a record has no
    component label; the report says it is derived, not declared."""
    m = re.match(r"([A-Za-z]+)", task_id or "")
    return m.group(1) if m else "?"


def failure_clusters(history: list[dict]) -> list[dict]:
    """(task_id, component, verdict) counts, most frequent first."""
    counts: Counter = Counter()
    for r in history:
        counts[(r.get("task_id", "?"),
                r.get("component") or "未标注",
                r.get("verdict", "?"))] += 1
    return [{"task_id": t, "component": c, "verdict": v, "n": n}
            for (t, c, v), n in counts.most_common()]


def component_win_rate(history: list[dict]) -> dict:
    """ADOPT rate per component label, plus the fallback grouping.

    Returns {"by_component": [...], "by_task_type": [...],
             "unlabelled": n}. "by_component" only covers records that
    carry a component; records without one are counted in "unlabelled"
    and grouped by task type instead (see task_type()).
    """
    by_comp: dict[str, dict] = defaultdict(lambda: {"n": 0, "adopt": 0})
    by_type: dict[str, dict] = defaultdict(lambda: {"n": 0, "adopt": 0})
    unlabelled = 0
    for r in history:
        verdict = r.get("verdict")
        comp = r.get("component")
        ttype = task_type(r.get("task_id", ""))
        by_type[ttype]["n"] += 1
        if verdict == "ADOPT":
            by_type[ttype]["adopt"] += 1
        if not comp:
            unlabelled += 1
            continue
        by_comp[comp]["n"] += 1
        if verdict == "ADOPT":
            by_comp[comp]["adopt"] += 1

    def _table(d: dict) -> list[dict]:
        rows = [{"group": k, "n": v["n"], "adopt": v["adopt"],
                 "adopt_rate": round(v["adopt"] / v["n"], 4) if v["n"] else 0.0}
                for k, v in d.items()]
        return sorted(rows, key=lambda x: (-x["adopt_rate"], -x["n"], x["group"]))

    return {"by_component": _table(by_comp),
            "by_task_type": _table(by_type),
            "unlabelled": unlabelled}


def flaky_tests(rewards: list[dict] | None) -> dict:
    """Tests whose tier B outcome flips across rounds.

    Each reward contributes one observation per test in its tier B
    per-test map (rsi/selection.test_map); observations are ordered by
    the reward's "round" key when present, otherwise by input order. A
    test is flaky when it is observed >= 2 times with both a pass and a
    fail. Returns {"available": False, "reason": ...} when no reward
    carries a per-test map.
    """
    seq: dict[str, list[bool]] = defaultdict(list)
    seen_rounds: dict[str, list] = defaultdict(list)
    for i, reward in enumerate(rewards or []):
        m = sel.test_map(reward, "B")
        if not m:
            continue
        rnd = reward.get("round", i)
        for test, ok in sorted(m.items()):
            seq[test].append(bool(ok))
            seen_rounds[test].append(rnd)
    flaky = []
    for test, outcomes in seq.items():
        if len(outcomes) < 2 or all(outcomes) or not any(outcomes):
            continue
        flips = sum(1 for a, b in zip(outcomes, outcomes[1:]) if a != b)
        flaky.append({"test": test,
                      "n_pass": sum(1 for o in outcomes if o),
                      "n_fail": sum(1 for o in outcomes if not o),
                      "flips": flips,
                      "rounds": seen_rounds[test],
                      "sequence": ["pass" if o else "fail" for o in outcomes]})
    flaky.sort(key=lambda x: (-x["flips"], -x["n_fail"], x["test"]))
    if not seq:
        return {"available": False,
                "reason": "no tier B per-test data: rewards carry per-test "
                          "maps only for go-test-json tiers; plain-command "
                          "tiers (e.g. self domain `pytest -q`) have none",
                "tests": []}
    return {"available": True, "reason": "", "tests": flaky}


def telemetry_summary(telemetry: list[dict]) -> dict:
    """Counts by kind and by polarity (feedback.polarity)."""
    kinds: Counter = Counter()
    polarities: Counter = Counter()
    for r in telemetry:
        kinds[r.get("kind", "?")] += 1
        p = fb.polarity(r)
        polarities["positive" if p > 0 else "negative" if p < 0 else "neutral"] += 1
    return {"n": len(telemetry),
            "by_kind": dict(kinds.most_common()),
            "by_polarity": dict(polarities)}


def cluster_to_draft(cluster: dict) -> dict | None:
    """One failure cluster -> one draft (or None: too little signal, or a
    verdict that must never become a task).

    The draft is produced by feeding a synthetic record to
    feedback.feedback_to_learning, so the artifact shape (kind / draft /
    title / suggested_dir / note) is the same one human feedback
    produces; only the provenance differs (source="analyzer").
    """
    if cluster["n"] < MIN_CLUSTER_FOR_DRAFT:
        return None
    label = CLUSTER_LABELS.get(cluster["verdict"])
    if label is None:
        return None
    note = (f"分析器：任务 {cluster['task_id']}（组件 "
            f"{cluster['component']}）连续 {cluster['n']} 次裁决为 "
            f"{cluster['verdict']}")
    rec = fb.FeedbackRecord(
        candidate_id=f"analyzer:{cluster['task_id']}",
        task_id=cluster["task_id"], kind="human_label", source="system",
        label=label, note=note)
    converted = fb.feedback_to_learning(rec)
    if converted["action"] == "weight_only":
        return None
    artifact = dict(converted["artifact"] or {})
    artifact["source"] = "analyzer"
    artifact["source_task"] = cluster["task_id"]
    artifact["source_verdict"] = cluster["verdict"]
    artifact["n_records"] = cluster["n"]
    return {"action": converted["action"], "artifact": artifact,
            "reason": converted["reason"] + f"（聚类：{note}）"}


def analyze(history: list[dict], telemetry: list[dict] | None = None,
            rewards: list[dict] | None = None) -> dict:
    """Full analysis over ledger records. Pure function over plain data.

    `history`: history.jsonl records; `telemetry`: telemetry.jsonl
    records (may be []); `rewards`: reward dicts (may be None -- the
    flaky section then reports no per-test data).
    """
    history = list(history or [])
    telemetry = list(telemetry or [])
    verdicts: Counter = Counter()
    for r in history:
        verdicts[r.get("verdict", "?")] += 1
    clusters = failure_clusters(history)
    drafts = [d for d in (cluster_to_draft(c) for c in clusters) if d]
    warnings = []
    for c in clusters:
        if c["verdict"] == "GAMING-DETECTED":
            warnings.append(f"作弊命中 {c['n']} 次：{c['task_id']}（组件 "
                            f"{c['component']}）-- 人工复核该候选，不自动成任务")
        if c["verdict"] == "STUCK":
            warnings.append(f"Fixer 卡住 {c['n']} 次：{c['task_id']} -- 先修 "
                            "fixer/接线，不是任务本身")
    return {
        "n_history": len(history),
        "n_telemetry": len(telemetry),
        "verdict_counts": dict(verdicts.most_common()),
        "failure_clusters": clusters,
        "component_win_rate": component_win_rate(history),
        "flaky_tests": flaky_tests(rewards),
        "telemetry": telemetry_summary(telemetry),
        "drafts": drafts,
        "warnings": warnings,
    }


def analyze_files(history_path: Path | str, telemetry_path: Path | str,
                  rewards_dir: Path | str | None = None) -> dict:
    """File-backed entry point: read the two ledgers (+ rewards, if a
    runs dir is given) and analyze them."""
    rewards = load_rewards(rewards_dir) if rewards_dir else None
    return analyze(load_jsonl(history_path), load_jsonl(telemetry_path),
                   rewards)


def report_markdown(report: dict) -> str:
    """Render an analysis dict as markdown text."""
    lines = ["# RSI 分析报告",
             "",
             f"账本记录 {report['n_history']} 条，遥测记录 "
             f"{report['n_telemetry']} 条。",
             "",
             "## 裁决总览",
             ""]
    if report["verdict_counts"]:
        for v, n in report["verdict_counts"].items():
            lines.append(f"- {v}：{n}")
    else:
        lines.append("- （无记录）")
    lines += ["", "## 失败模式聚类（任务 / 组件 / 裁决）", ""]
    if report["failure_clusters"]:
        lines.append("| 任务 | 组件 | 裁决 | 次数 |")
        lines.append("| --- | --- | --- | ---: |")
        for c in report["failure_clusters"]:
            lines.append(f"| {c['task_id']} | {c['component']} | "
                         f"{c['verdict']} | {c['n']} |")
    else:
        lines.append("- （无记录）")
    lines += ["", "## 组件胜率（ADOPT 率）", ""]
    cwr = report["component_win_rate"]
    if cwr["by_component"]:
        lines.append("| 组件 | 候选数 | 采纳数 | ADOPT 率 |")
        lines.append("| --- | ---: | ---: | ---: |")
        for r in cwr["by_component"]:
            lines.append(f"| {r['group']} | {r['n']} | {r['adopt']} | "
                         f"{r['adopt_rate']:.2%} |")
    else:
        lines.append("- （无带组件标签的记录）")
    if cwr["unlabelled"]:
        lines.append("")
        lines.append(f"注：{cwr['unlabelled']} 条记录没有组件标签，按任务类型"
                     "聚类如下（任务类型取 task_id 前缀，是派生分组不是"
                     "声明的组件）：")
        lines.append("")
        lines.append("| 任务类型 | 候选数 | 采纳数 | ADOPT 率 |")
        lines.append("| --- | ---: | ---: | ---: |")
        for r in cwr["by_task_type"]:
            lines.append(f"| {r['group']} | {r['n']} | {r['adopt']} | "
                         f"{r['adopt_rate']:.2%} |")
    lines += ["", "## 抖动测试（tier B 逐测试结果，多轮翻转）", ""]
    fl = report["flaky_tests"]
    if not fl["available"]:
        lines.append(f"- （{fl['reason']}）")
    elif not fl["tests"]:
        lines.append("- 无抖动测试：所有逐测试结果在多轮里保持一致。")
    else:
        lines.append("| 测试 | 通过 | 失败 | 翻转次数 | 轮次序列 |")
        lines.append("| --- | ---: | ---: | ---: | --- |")
        for t in fl["tests"]:
            lines.append(f"| {t['test']} | {t['n_pass']} | {t['n_fail']} | "
                         f"{t['flips']} | {' → '.join(t['sequence'])} |")
    lines += ["", "## 遥测信号", ""]
    tel = report["telemetry"]
    if tel["n"]:
        lines.append(f"共 {tel['n']} 条；按 kind："
                     + "、".join(f"{k} {v}" for k, v in tel["by_kind"].items())
                     + "；按极性："
                     + "、".join(f"{k} {v}" for k, v in tel["by_polarity"].items()))
    else:
        lines.append("- （无遥测记录）")
    lines += ["", "## 草案（只产草案，不自动写进 evolve.json）", ""]
    if report["drafts"]:
        for d in report["drafts"]:
            a = d["artifact"]
            lines.append(f"- [{d['action']}] {a.get('title', '')}（来源："
                         f"{a.get('source_task', '')}，"
                         f"{a.get('n_records', '')} 条记录，"
                         f"裁决 {a.get('source_verdict', '')}）")
    else:
        lines.append("- 无草案：没有达到 2 条记录的失败簇。")
    if report["warnings"]:
        lines += ["", "## 注意", ""]
        for w in report["warnings"]:
            lines.append(f"- {w}")
    return "\n".join(lines) + "\n"


def write_report(report: dict, path: Path | str) -> Path:
    """Render the report as markdown and write it to `path`."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(report_markdown(report), encoding="utf-8")
    return p
