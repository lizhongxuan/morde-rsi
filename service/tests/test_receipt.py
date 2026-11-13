import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from service.receipt import build_receipt, compute_usd, write_receipt


def test_compute_usd_math():
    # 1M input tokens at $1/1M + 500k output tokens at $2/1M = $2.00.
    assert compute_usd(1_000_000, 500_000, 1.0, 2.0) == 2.0
    assert compute_usd(0, 0, 1.0, 2.0) == 0.0
    assert compute_usd(100, 200, 0.5, 0.25) == pytest.approx(0.0001)
    # Fractional / int prices both work.
    assert compute_usd(2_000_000, 0, 3, 5) == 6.0


def test_compute_usd_none_prices():
    assert compute_usd(1000, 1000, None, 2.0) is None
    assert compute_usd(1000, 1000, 1.0, None) is None
    assert compute_usd(1000, 1000, None, None) is None


def test_build_receipt_exact_keys():
    receipt = build_receipt(
        "abc123", tokens_in=100, tokens_out=50, usd=0.001,
        wall_seconds=12.5, verdict="ADOPT",
        branch="rsi/CAND-001", pr_url="https://example.invalid/pr/1",
    )
    assert set(receipt) == {"job_id", "tokens_in", "tokens_out", "usd",
                            "wall_seconds", "verdict", "branch", "pr_url"}
    assert receipt["job_id"] == "abc123"
    assert receipt["tokens_in"] == 100
    assert receipt["tokens_out"] == 50
    assert receipt["usd"] == 0.001
    assert receipt["wall_seconds"] == 12.5
    assert receipt["verdict"] == "ADOPT"
    assert receipt["branch"] == "rsi/CAND-001"
    assert receipt["pr_url"] == "https://example.invalid/pr/1"


def test_build_receipt_defaults():
    receipt = build_receipt("job-1")
    assert receipt == {"job_id": "job-1", "tokens_in": 0, "tokens_out": 0,
                       "usd": None, "wall_seconds": 0.0, "verdict": None,
                       "branch": None, "pr_url": None}


def test_write_receipt_round_trip(tmp_path):
    data_dir = tmp_path / "data"
    receipt = build_receipt("job-42", tokens_in=10, tokens_out=5,
                            usd=0.00003, wall_seconds=3.2,
                            verdict="REJECT", branch=None, pr_url=None)
    path = write_receipt(data_dir, "job-42", receipt)
    assert path == data_dir / "receipts" / "job-42.json"
    assert path.is_file()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == receipt


def test_write_receipt_creates_parents(tmp_path):
    path = write_receipt(tmp_path / "deep" / "data", "j1", build_receipt("j1"))
    assert path.parent.is_dir()
    assert json.loads(path.read_text())["job_id"] == "j1"
