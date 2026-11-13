"""Evidence ledger: one JSONL record per candidate.

Borrowed from RRSI rrsi/history.py (one JSONL record per edit; ts auto-filled;
measured vs unmeasured records; summaries over the trajectory). Changed: the
record unit is one candidate fixing one bug, and the fields carry bug-fix
semantics (task_id, diff hash, per-tier results, verdict, baseline commit,
cost) instead of harness-edit semantics.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

VERDICTS = ("ADOPT", "REJECT", "STUCK", "GAMING-DETECTED", "SKIPPED",
            "NEEDS_HUMAN")


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
                         fixer: str = "", detail: str = "") -> None:
        """Write the ledger record of one candidate."""
        self.append({
            "round": round_no,
            "task_id": task_id,
            "candidate_id": candidate_id,
            "variant": variant,
            "component": component,
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
                "hypothesis", "verdict", "score", "baseline_commit",
                "cost_seconds", "fixer", "detail")
        return [{k: r.get(k) for k in keys if r.get(k) is not None}
                for r in recs]
