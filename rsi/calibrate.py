"""Noise-band (delta) estimation from repeated baseline evaluations.

Ported structure + formulas from RRSI refs/rrsi/rrsi/calibrate.py
(bootstrap_se / pooled / calibrate; Apache-2.0, RRSI main 2026-09-21).
Changed:

* The input is morde-rsi's own EvalResult (rsi/selection.py); nothing is
  imported from RRSI's evaluate.py.
* calibrate() raises ValueError when the pooled evaluation has no trials
  at all: a delta of 0 from empty data would be a fabricated number, not
  a measurement.
* Added the runs-dir I/O the CLI needs: load_baseline_evals() reads
  baseline-eval-*.json (EvalResult JSON) and baseline-reward-*.json
  (verifier reward.json, converted with selection.eval_from_reward);
  write_calibration() / read_calibration() own runs/<domain>/
  calibration.json; record_baseline_eval() writes one EvalResult for
  later calibration. rrsi.json itself is never rewritten: a hand-edited
  config must not be overwritten by a measurement.

delta bounds |S_hat(H) - S_hat(H)| between two independent evaluations of
the SAME harness. With R >= 2 independent base evaluations the null
difference is observed directly; with a single k-trial evaluation the
standard error of S_hat is bootstrapped over trials within each
check-group, and the null difference of two independent evaluations has
standard deviation sqrt(2) * se. Either way delta = z * sd(null Delta S),
z = cfg.delta_z (2.0 by default).
"""
from __future__ import annotations

import json
import math
import random
import statistics as st
from pathlib import Path

from .selection import EvalResult, TaskResult, aggregate, eval_from_reward


def bootstrap_se(ev: EvalResult, reps: int = 2000, seed: int = 7) -> float:
    """se(S_hat) by resampling trials within each check-group (weights respected).

    From RRSI refs/rrsi/rrsi/calibrate.py::bootstrap_se, math unchanged:
    each replicate resamples, with replacement, the trials inside every
    group and recomputes the weighted mean; the standard deviation of the
    replicates is the standard error. Fixed seed -> reproducible.
    """
    rng = random.Random(seed)
    tasks = [tr for tr in ev.per_task.values() if tr.rewards]
    vals = []
    for _ in range(reps):
        num = den = 0.0
        for tr in tasks:
            n = len(tr.rewards)
            for _j in range(n):
                i = rng.randrange(n)
                num += tr.rewards[i] * tr.weights[i]
                den += tr.weights[i]
        vals.append(num / den if den else 0.0)
    return st.pstdev(vals) if len(vals) > 1 else 0.0


def pooled(evals: list[EvalResult]) -> EvalResult:
    """Concatenate the trials of several evaluations of the same harness.

    From RRSI refs/rrsi/rrsi/calibrate.py::pooled; the only change is that
    aggregate comes from rsi/selection.py (morde's own copy).
    """
    per: dict = {}
    for ev in evals:
        for t, tr in ev.per_task.items():
            p = per.setdefault(t, TaskResult(rewards=[], weights=[], tokens=[]))
            p.rewards += tr.rewards
            p.weights += tr.weights
            p.tokens += tr.tokens
            p.missing += tr.missing
    return aggregate("pooled", sum(e.k for e in evals), per)


def calibrate(evals: list[EvalResult], z: float = 2.0,
              reps: int = 2000) -> dict:
    """delta from one or more evaluations of the base harness.

    Branching copied from RRSI refs/rrsi/rrsi/calibrate.py::calibrate:
    with >= 2 independent base evaluations the null difference is observed
    directly (sd of the scores * sqrt(2)); with one evaluation the
    standard error is bootstrapped and the null difference has sd
    sqrt(2) * se. The direct observation wins unless it is degenerate
    (sd 0, e.g. all evaluations scored identically), in which case the
    bootstrap value is used instead. Either way delta = z * sd(null Delta S).

    Raises ValueError on no evaluations, or when the pooled evaluation has
    no trials at all (see module docstring: no fabricated delta).
    """
    if not evals:
        raise ValueError("no base evaluations")
    if len(evals) >= 2:
        scores = [e.S for e in evals]
        diffs = [abs(a - b) for i, a in enumerate(scores)
                 for b in scores[i + 1:]]
        sd_null = st.stdev(scores) * math.sqrt(2) if len(scores) > 1 else 0.0
        method = "repeated base evaluations"
        observed = {"S_per_eval": scores, "max_abs_diff": max(diffs)}
    else:
        sd_null = 0.0
        method = "bootstrap over trials of one base evaluation"
        observed = {}
    pooled_ev = pooled(evals)
    if not any(tr.rewards for tr in pooled_ev.per_task.values()):
        raise ValueError("no trials in base evaluations")
    se = bootstrap_se(pooled_ev, reps=reps)
    sd_boot = math.sqrt(2) * se * math.sqrt(pooled_ev.k / evals[0].k)
    # With repeated evals prefer the direct observation unless degenerate.
    sd_use = sd_null if (len(evals) >= 2 and sd_null > 0) else sd_boot
    return {"delta": round(z * sd_use, 6), "z": z, "sd_null": round(sd_use, 6),
            "sd_null_bootstrap": round(sd_boot, 6),
            "se_bootstrap": round(se, 6),
            "method": method, "n_evals": len(evals), "k": evals[0].k,
            "n_tasks": len(pooled_ev.per_task),
            "S_base": round(evals[0].S, 6),
            "C_base": evals[0].C, **observed}


# ------------------------------------------------------------ runs-dir I/O --

def _load_eval_file(path: Path) -> EvalResult | None:
    """One baseline file -> EvalResult, or None when it is not one.

    Accepts two shapes: an EvalResult JSON (per_task + S, as written by
    record_baseline_eval) and a verifier reward.json (tiers of checks, as
    written by scripts/verify.py --out), converted with
    selection.eval_from_reward. Anything else (frontier.json, approval
    cards, calibration.json) is not a baseline evaluation and is skipped.
    """
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if "per_task" in data and "S" in data:
        try:
            return EvalResult.from_json(data)
        except (KeyError, TypeError, ValueError):
            return None
    tiers = data.get("tiers")
    if isinstance(tiers, dict) and tiers and all(
            isinstance(v, dict) and "checks" in v for v in tiers.values()):
        return eval_from_reward(data.get("task_id", "?"), data,
                                job=path.stem)
    return None


def load_baseline_evals(runs_dir: Path | str) -> list[EvalResult]:
    """Baseline evaluations already saved under runs/<domain>/.

    Files: baseline-eval-*.json (EvalResult JSON) and
    baseline-reward-*.json (verifier reward.json). Sorted by file name so
    the order is deterministic. Unparseable files and evaluations with no
    trials carry no information and are skipped. Returns [] when there is
    nothing usable -- the caller must not invent a delta from that.
    """
    runs_dir = Path(runs_dir)
    evals: list[EvalResult] = []
    if not runs_dir.is_dir():
        return evals
    for path in sorted(runs_dir.glob("baseline-*.json")):
        ev = _load_eval_file(path)
        if ev is not None and any(tr.rewards
                                  for tr in ev.per_task.values()):
            evals.append(ev)
    return evals


def record_baseline_eval(runs_dir: Path | str, ev: EvalResult,
                         task_id: str) -> Path:
    """Save one baseline EvalResult as baseline-eval-<task>-<n>.json.

    For the controller (or an operator) to call after a baseline run, so
    a later `calibrate` has repeated evaluations to read. Never overwrites
    an existing file: the index n is the first free one.
    """
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    i = 1
    while (runs_dir / f"baseline-eval-{task_id}-{i}.json").exists():
        i += 1
    path = runs_dir / f"baseline-eval-{task_id}-{i}.json"
    path.write_text(json.dumps(ev.to_json(), indent=1))
    return path


def write_calibration(runs_dir: Path | str, cal: dict) -> Path:
    """Write the measured delta to runs/<domain>/calibration.json.

    Deliberately not rrsi.json: a hand-edited config must not be
    overwritten by a measurement. The CLI adopts this file when
    rrsi.json's delta is null.
    """
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / "calibration.json"
    path.write_text(json.dumps(cal, indent=1))
    return path


def read_calibration(runs_dir: Path | str) -> float | None:
    """Measured delta from runs/<domain>/calibration.json, or None."""
    p = Path(runs_dir) / "calibration.json"
    if not p.exists():
        return None
    try:
        return float(json.loads(p.read_text())["delta"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return None


def auto_calibrate(runs_dir: Path | str, z: float = 2.0,
                   reps: int = 2000) -> tuple[dict, Path]:
    """Calibrate from the baseline evaluations in runs_dir and persist it.

    Returns (calibration dict, calibration.json path). Raises ValueError
    when there are no usable baseline evaluations -- the caller must
    report that and keep delta unmeasured, never invent one.
    """
    runs_dir = Path(runs_dir)
    evals = load_baseline_evals(runs_dir)
    if not evals:
        raise ValueError(
            f"no baseline evaluations in {runs_dir}: expected "
            "baseline-eval-*.json (EvalResult JSON) or baseline-reward-*.json "
            "(verifier reward.json); run the verifier on the baseline "
            "twice and save the outputs there, then re-run calibrate")
    cal = calibrate(evals, z=z, reps=reps)
    return cal, write_calibration(runs_dir, cal)
