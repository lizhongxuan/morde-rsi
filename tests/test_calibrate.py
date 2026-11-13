"""Calibrate: noise-band re-estimation from repeated baseline evaluations.

Ported from RRSI refs/rrsi/rrsi/calibrate.py (bootstrap_se / pooled /
calibrate); the inputs here are morde-rsi EvalResults with hand-written,
known-variance scores -- nothing is read from the Morde repo.
"""
import json
import math
import statistics as st

import pytest

from rsi.calibrate import (auto_calibrate, bootstrap_se, calibrate,
                           load_baseline_evals, pooled, read_calibration,
                           record_baseline_eval, write_calibration)
from rsi.selection import EvalResult, TaskResult


def _ev(s, rewards, weights=None, k=1, job="base"):
    """An EvalResult whose per-task rewards are exactly `rewards`."""
    weights = weights or [1.0] * len(rewards)
    return EvalResult(job=job, k=k,
                      per_task={"checks": TaskResult(rewards=list(rewards),
                                                     weights=list(weights))},
                      S=s, C=None, n_expected=len(rewards) * k, missing=0)


def test_bootstrap_se_known_variance():
    """One group, rewards [1, 0]: a bootstrap replicate (mean of 2 draws
    with replacement) is 0 / 0.5 / 1 with probabilities 1/4 / 1/2 / 1/4,
    so its population sd is sqrt(0.125) ~= 0.3536."""
    ev = _ev(0.5, [1.0, 0.0])
    se = bootstrap_se(ev, reps=2000)
    assert se == pytest.approx(math.sqrt(0.125), abs=0.05)


def test_bootstrap_se_deterministic_same_seed():
    ev = _ev(0.5, [1.0, 0.0, 1.0, 0.0, 1.0])
    assert bootstrap_se(ev, reps=500) == bootstrap_se(ev, reps=500)
    assert calibrate([ev], reps=500) == calibrate([ev], reps=500)


def test_calibrate_single_eval_uses_bootstrap():
    """One evaluation -> bootstrap branch; delta = z * sqrt(2) * se."""
    ev = _ev(0.5, [1.0, 0.0])
    cal = calibrate([ev], z=2.0, reps=2000)
    assert cal["method"] == "bootstrap over trials of one base evaluation"
    assert cal["n_evals"] == 1 and cal["k"] == 1
    # delta = z * sqrt(2) * se = 2 * sqrt(2) * sqrt(0.125) = 1.0.
    assert cal["delta"] == pytest.approx(1.0, abs=0.15)
    assert cal["S_base"] == 0.5


def test_calibrate_two_evals_uses_observed_spread():
    """Two evaluations with S 0.5 and 0.7: sd(scores)=sqrt(0.02), so
    sd_null = sqrt(0.02)*sqrt(2) = 0.2 and delta = z * 0.2 = 0.4."""
    ev1 = _ev(0.5, [1.0, 0.0])
    ev2 = _ev(0.7, [1.0, 1.0, 0.0])
    cal = calibrate([ev1, ev2], z=2.0)
    assert cal["method"] == "repeated base evaluations"
    assert cal["n_evals"] == 2
    assert cal["S_per_eval"] == [0.5, 0.7]
    assert cal["max_abs_diff"] == pytest.approx(0.2)
    assert cal["delta"] == pytest.approx(0.4, abs=1e-6)


def test_calibrate_two_identical_evals_falls_back_to_bootstrap():
    """Two evaluations scoring identically: the observed spread is 0
    (degenerate), so the bootstrap value is used instead -- the branch
    is chosen by the data, not by the count alone."""
    ev1 = _ev(0.6, [1.0, 1.0, 0.0])
    ev2 = _ev(0.6, [1.0, 0.0, 1.0])
    cal = calibrate([ev1, ev2], z=2.0, reps=2000)
    assert cal["method"] == "repeated base evaluations"
    assert cal["sd_null"] == pytest.approx(cal["sd_null_bootstrap"], abs=1e-6)
    # sd_boot = sqrt(2) * se * sqrt(pooled.k / evals[0].k); pooled k=2,
    # first eval k=1, so the factor is sqrt(2)*sqrt(2) = 2.
    p = pooled([ev1, ev2])
    expected = 2.0 * math.sqrt(2) * bootstrap_se(p, reps=2000) \
        * math.sqrt(p.k / ev1.k)
    assert cal["delta"] == pytest.approx(expected, abs=0.15)


def test_calibrate_empty_input_raises():
    with pytest.raises(ValueError, match="no base evaluations"):
        calibrate([])


def test_calibrate_no_trials_raises():
    ev = EvalResult(job="base", k=1, per_task={}, S=0.0, C=None,
                    n_expected=0, missing=0)
    with pytest.raises(ValueError, match="no trials"):
        calibrate([ev])


def test_pooled_concatenates_trials():
    ev1 = _ev(0.5, [1.0, 0.0], k=1)
    ev2 = _ev(1.0, [1.0, 1.0], k=2)
    p = pooled([ev1, ev2])
    assert p.per_task["checks"].rewards == [1.0, 0.0, 1.0, 1.0]
    assert p.k == 3
    assert p.S == pytest.approx(0.75)


def test_load_baseline_evals_reads_both_shapes(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    # EvalResult JSON shape.
    ev = _ev(0.5, [1.0, 0.0])
    (runs / "baseline-eval-T-1.json").write_text(json.dumps(ev.to_json()))
    # Verifier reward.json shape (converted via eval_from_reward).
    reward = {"task_id": "T", "tiers": {
        "A": {"passed": True, "status": "run", "checks": [
            {"passed": True, "tests": {}}]},
        "B": {"passed": True, "status": "run", "checks": [
            {"passed": True, "tests": {}}]},
    }}
    (runs / "baseline-reward-T-1.json").write_text(json.dumps(reward))
    # Not baseline evaluations: skipped, not errors.
    (runs / "frontier.json").write_text(json.dumps({"score": 1.0}))
    (runs / "baseline-broken.json").write_text("{not json")
    (runs / "calibration.json").write_text(json.dumps({"delta": 0.03}))
    evals = load_baseline_evals(runs)
    assert len(evals) == 2
    assert [e.S for e in evals] == [0.5, 1.0]  # filename order
    assert load_baseline_evals(tmp_path / "missing-dir") == []


def test_record_and_read_calibration_roundtrip(tmp_path):
    runs = tmp_path / "runs"
    ev = _ev(0.5, [1.0, 0.0])
    p1 = record_baseline_eval(runs, ev, "T")
    p2 = record_baseline_eval(runs, ev, "T")
    assert p1.name == "baseline-eval-T-1.json"
    assert p2.name == "baseline-eval-T-2.json"
    assert len(load_baseline_evals(runs)) == 2
    assert read_calibration(runs) is None
    cal = calibrate(load_baseline_evals(runs), reps=500)
    path = write_calibration(runs, cal)
    assert path == runs / "calibration.json"
    assert read_calibration(runs) == pytest.approx(cal["delta"])


def test_auto_calibrate_no_data_raises(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    with pytest.raises(ValueError, match="no baseline evaluations"):
        auto_calibrate(runs)


def test_auto_calibrate_writes_calibration_json(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    record_baseline_eval(runs, _ev(0.5, [1.0, 0.0]), "T")
    record_baseline_eval(runs, _ev(0.7, [1.0, 1.0, 0.0]), "T")
    cal, path = auto_calibrate(runs)
    assert cal["delta"] == pytest.approx(0.4, abs=1e-6)
    assert path == runs / "calibration.json"
    assert read_calibration(runs) == pytest.approx(0.4, abs=1e-6)
    # The bootstrap standard error is also reported, for the record.
    assert cal["se_bootstrap"] >= 0
