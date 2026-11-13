"""Evidence ledger: one JSONL record per candidate.

Borrowed from RRSI rrsi/history.py (one JSONL record per edit; ts auto-filled;
measured vs unmeasured records; summaries over the trajectory). Changed: the
record unit is one candidate fixing one bug, and the fields carry bug-fix
semantics (task_id, diff hash, per-tier results, verdict, baseline commit,
cost) instead of harness-edit semantics.

Stall / exploration / prune (stall_flag, exploration, tried, prune_set) are
ported from the same RRSI file (same formulas, same return shapes); the
prune rule itself is Morde's own (see prune_set), and the vocabulary is
Morde's K (rsi/components.py), not RRSI's harness K.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .components import K

VERDICTS = ("ADOPT", "REJECT", "STUCK", "GAMING-DETECTED", "SKIPPED",
            "NEEDS_HUMAN")

# Verdicts that carry a measurement (a score the loop can learn from).
# SKIPPED (bug did not reproduce) and STUCK / GAMING-DETECTED (no clean
# evaluation) are not measurements: they must not enter T_t, g_t or the
# stall trajectory.
MEASURED_VERDICTS = ("ADOPT", "REJECT", "NEEDS_HUMAN")


# --------------------------------------------------------------- explore --
def stall_flag(trajectory: list[float], t: int, w: int, delta: float) -> int:
    """sigma_t = 1[S_t - S_{t-w} <= delta]; 0 while fewer than w rounds exist.

    Ported verbatim from RRSI rrsi/history.py::stall_flag (formula
    unchanged). ``trajectory`` is the incumbent score after each round,
    in round order (see History.trajectory()); ``t`` is the index of the
    current round in that list.
    """
    if t < w or t >= len(trajectory) or t - w < 0:
        return 0
    return int(trajectory[t] - trajectory[t - w] <= delta)


def exploration(t: int, stall: int, tried: set[str], m_draft: int,
                vocabulary: list[str] | None = None) -> dict:
    """E_t = (sigma_t, U_t, m_draft) plus the text handed to the proposer.

    Ported from RRSI rrsi/history.py::exploration: same shape and keys
    (``sigma`` / ``untried`` / ``m_draft`` / ``text``), untried computed
    over Morde's K by default; the text is rewritten in Chinese for Morde.
    `vocabulary` overrides K for other domains (the self domain has its
    own small vocabulary: config-default / logic-small / docs -- without
    this the self loop would be told to explore Morde-only components
    such as portal-ui that do not exist in its repo).
    """
    vocab = vocabulary or K
    untried = [c for c in vocab if c not in tried]
    if stall and untried:
        text = (f"停滞：基线分数在最近几轮里没有超出噪声带（sigma_t = 1）。"
                f"本轮保留 {m_draft} 个候选位置给从未试过的组件：{untried}。"
                f"占保留位的候选必须至少改动其中一个组件。")
    elif untried:
        text = (f"尚未试过的组件：{untried}。本轮不强制（sigma_t = 0），"
                f"但这些组件仍无证据。")
    else:
        text = "K 中的每个组件都至少试过一次。"
    return {"sigma": stall, "untried": untried, "m_draft": m_draft,
            "text": text}


class History:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    # ------------------------------------------------------------ storage --
    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def append(self, rec: dict) -> None:
        rec = dict(rec)
        rec.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def append_candidate(self, round_no: int, task_id: str, candidate_id: str,
                         variant: str, component: str | None,
                         hypothesis: str | None, diff: str | None,
                         tiers: dict | None, verdict: str,
                         baseline_commit: str | None, score: float | None,
                         cost_tokens: int | None, cost_seconds: float | None,
                         fixer: str = "", detail: str = "",
                         components: list[str] | None = None) -> None:
        """Write the ledger record of one candidate.

        ``component`` (single str) is kept for compatibility with older
        records and callers; ``components`` is the full label list (one
        entry per candidate in the pilot). When ``components`` is given
        it is stored as-is; ``component`` falls back to its first entry.
        """
        comps = ([str(c) for c in components] if components else
                 ([component] if component else []))
        self.append({
            "round": round_no,
            "task_id": task_id,
            "candidate_id": candidate_id,
            "variant": variant,
            "component": component or (comps[0] if comps else None),
            "components": comps,
            "hypothesis": hypothesis,
            "diff_sha256": (hashlib.sha256(diff.encode()).hexdigest()
                            if diff else None),
            "tiers": tiers or {},
            "verdict": verdict,
            "baseline_commit": baseline_commit,
            "score": None if score is None else round(score, 6),
            "cost_tokens": cost_tokens,
            "cost_seconds": (None if cost_seconds is None
                             else round(cost_seconds, 1)),
            "fixer": fixer,
            "detail": detail[:600] if detail else "",
        })

    # ---------------------------------------------------------- summaries --
    def attempted_tasks(self) -> set[str]:
        return {r["task_id"] for r in self.records() if r.get("task_id")}

    def latest_record(self, task_id: str) -> dict | None:
        """Most recent ledger record for one task ({}'s verdict decides
        whether the task is still open)."""
        recs = [r for r in self.records() if r.get("task_id") == task_id]
        return recs[-1] if recs else None

    def latest_verdict(self, task_id: str) -> str | None:
        rec = self.latest_record(task_id)
        return rec.get("verdict") if rec else None

    @staticmethod
    def record_components(rec: dict) -> list[str]:
        """Component labels of one record: the ``components`` list when
        present, else the legacy single ``component`` field."""
        comps = rec.get("components")
        if isinstance(comps, list) and comps:
            return [c for c in comps if isinstance(c, str) and c]
        c = rec.get("component")
        return [c] if isinstance(c, str) and c else []

    def measured(self) -> list[dict]:
        """Records carrying a measurement: a score plus a verdict the loop
        can learn from (ADOPT / REJECT / NEEDS_HUMAN). RRSI's measured()
        is delta_S is not None; morde-rsi has no delta_S field, so the
        verdict + score play that role. SKIPPED / STUCK / GAMING-DETECTED
        never enter T_t, g_t or the stall trajectory."""
        return [r for r in self.records()
                if r.get("verdict") in MEASURED_VERDICTS
                and r.get("score") is not None]

    def tried(self) -> set[str]:
        """T_t (RRSI history.py): components with at least one measured
        candidate, over Morde's K."""
        return {c for r in self.measured()
                for c in self.record_components(r) if c in K}

    def trajectory(self) -> list[float]:
        """Incumbent score after each round, in round order.

        A round that adopts moves the incumbent to the adopted score; any
        other round leaves it unchanged. One entry per round that has a
        record, so the index of an entry is its position in round order --
        callers pass ``t = len(trajectory)`` (the current round) to
        stall_flag with the current incumbent appended (see
        controller._stall_state). Starts at 0.0: no adoption yet means no
        measured gain, which is the honest baseline for a pilot.
        """
        incumbent = 0.0
        by_round: dict[int, list[dict]] = {}
        for r in self.records():
            by_round.setdefault(r.get("round", 0), []).append(r)
        out = []
        for rnd in sorted(by_round):
            for r in by_round[rnd]:
                if (r.get("verdict") == "ADOPT"
                        and r.get("score") is not None):
                    incumbent = float(r["score"])
            out.append(round(incumbent, 6))
        return out

    def component_summary(self) -> dict[str, dict]:
        """Per-component ADOPT / REJECT counts and win rate, for the
        analyser and the status command. ``attempts`` counts every record
        carrying the component (including STUCK / NEEDS_HUMAN); ``decided``
        counts only ADOPT + REJECT; ``win_rate`` is ADOPT / decided, or
        None when nothing has been decided yet. Old records without a
        ``components`` list are counted via their ``component`` field."""
        out: dict[str, dict] = {}
        for r in self.records():
            for c in self.record_components(r):
                e = out.setdefault(c, {"attempts": 0, "adopt": 0,
                                       "reject": 0, "decided": 0,
                                       "win_rate": None})
                e["attempts"] += 1
                if r.get("verdict") == "ADOPT":
                    e["adopt"] += 1
                    e["decided"] += 1
                elif r.get("verdict") == "REJECT":
                    e["reject"] += 1
                    e["decided"] += 1
        for e in out.values():
            if e["decided"]:
                e["win_rate"] = round(e["adopt"] / e["decided"], 4)
        return out

    def stuck_components(self, min_consecutive: int = 2) -> set[str]:
        """Components whose last ``min_consecutive`` decided outcomes are
        all REJECT (an ADOPT in between resets the run). Only decided
        verdicts count: STUCK is a fixer failure and NEEDS_HUMAN is a
        question for a human, neither is evidence that the component
        itself is exhausted. Used by the controller to skip stuck task
        types while stalled."""
        by_comp: dict[str, list[str]] = {}
        for r in self.records():
            for c in self.record_components(r):
                by_comp.setdefault(c, []).append(r.get("verdict"))
        stuck = set()
        for c, verdicts in by_comp.items():
            decided = [v for v in verdicts if v in ("ADOPT", "REJECT")]
            n = 0
            for v in reversed(decided):
                if v == "REJECT":
                    n += 1
                else:
                    break
            if n >= min_consecutive:
                stuck.add(c)
        return stuck

    def prune_set(self, t: int, n_prune: int) -> list[dict]:
        """B_t, Morde's rule (NOT RRSI's g_t(l) <= 0).

        RRSI prunes a component when its best recent gain is <= 0; that
        needs a per-component delta_S series, which morde-rsi does not
        keep (one candidate per task, one score per round). Morde's rule
        instead: look at the ``n_prune`` rounds immediately before round
        ``t`` (window ``[t - n_prune, t)``); a component is pruned when it
        was adopted 0 times in that window AND was decided (ADOPT/REJECT)
        at least twice. The "at least twice" floor is deliberate: one
        rejection is noise (a single bad fix, a flaky check), two
        rejections with zero adoptions in a 4-round window is a pattern
        worth steering away from -- with pilot T=5 the bar must be
        reachable but not trigger-happy. Sorted by attempts desc (most
        wasted effort first), then name; at most ``n_prune`` entries.
        Each entry carries the component and its attempt count.
        """
        lo = max(0, t - n_prune)
        attempts: dict[str, int] = {}
        adopted: dict[str, int] = {}
        for r in self.records():
            rnd = r.get("round")
            if not isinstance(rnd, int) or not (lo <= rnd < t):
                continue
            if r.get("verdict") not in ("ADOPT", "REJECT"):
                continue
            for c in self.record_components(r):
                attempts[c] = attempts.get(c, 0) + 1
                if r.get("verdict") == "ADOPT":
                    adopted[c] = adopted.get(c, 0) + 1
        out = [{"component": c, "attempts": n, "adopted": 0,
                "window_rounds": n_prune}
               for c, n in sorted(attempts.items(),
                                  key=lambda kv: (-kv[1], kv[0]))
               if adopted.get(c, 0) == 0 and n >= 2]
        return out[:n_prune]

    def accepted(self) -> list[dict]:
        return [r for r in self.records() if r.get("verdict") == "ADOPT"]

    def frontier(self) -> dict:
        """Current baseline: the last adopted candidate's commit + score.

        Returns {} when nothing has been adopted yet; the caller then uses
        the repo's HEAD as the baseline commit.
        """
        acc = self.accepted()
        if not acc:
            return {}
        last = acc[-1]
        return {"baseline_commit": last.get("baseline_commit"),
                "score": last.get("score"),
                "task_id": last.get("task_id"),
                "candidate_id": last.get("candidate_id"),
                "n_accepted": len(acc)}

    def render(self, n: int = 40) -> list[dict]:
        """Compact view of the most recent records."""
        recs = self.records()[-n:]
        keys = ("ts", "round", "task_id", "candidate_id", "component",
                "components", "hypothesis", "verdict", "score",
                "baseline_commit", "cost_seconds", "fixer", "detail")
        return [{k: r.get(k) for k in keys if r.get(k) is not None}
                for r in recs]
