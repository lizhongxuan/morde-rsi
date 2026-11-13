"""Cost receipts for the morde-rsi HTTP service (stdlib only).

A receipt is the per-job cost record: token counts, USD cost (when prices
are known), wall time, verdict, branch and PR link. Receipts are written
as JSON under ``<data_dir>/receipts/<job_id>.json``.
"""
from __future__ import annotations

import json
from pathlib import Path

_RECEIPT_KEYS = (
    "job_id",
    "tokens_in",
    "tokens_out",
    "usd",
    "wall_seconds",
    "verdict",
    "branch",
    "pr_url",
)


def compute_usd(tokens_in, tokens_out, price_in, price_out) -> float | None:
    """USD cost from token counts and per-1M-token prices.

    Returns None when either price is None (cost cannot be computed).
    """
    if price_in is None or price_out is None:
        return None
    return float(tokens_in) / 1_000_000 * float(price_in) + float(
        tokens_out
    ) / 1_000_000 * float(price_out)


def build_receipt(job_id, tokens_in=0, tokens_out=0, usd=None,
                  wall_seconds=0.0, verdict=None, branch=None,
                  pr_url=None) -> dict:
    """Build a receipt dict with exactly the receipt keys."""
    return {
        "job_id": job_id,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usd": usd,
        "wall_seconds": wall_seconds,
        "verdict": verdict,
        "branch": branch,
        "pr_url": pr_url,
    }


def write_receipt(data_dir, job_id, receipt) -> Path:
    """Write ``receipt`` to ``<data_dir>/receipts/<job_id>.json``."""
    path = Path(data_dir) / "receipts" / f"{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path
