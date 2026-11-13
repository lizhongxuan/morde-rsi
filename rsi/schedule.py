"""Annealed edit budget, RRSI schedule.py Eq. (anneal), formula unchanged:

    b_t = ceil( b_min + (b_max - b_min) * 1/2 (1 + cos(pi t / T)) ),  t = 0..T-1

Early rounds may bundle several coordinated edits in one candidate; late
rounds become sparse and attributable.
"""
from __future__ import annotations

import math


def edit_budget(t: int, T: int, b_min: int, b_max: int) -> int:
    """b_t for round t (0-indexed) of a T-round run."""
    if T <= 0:
        return int(b_max)
    t = max(0, min(int(t), int(T)))
    v = b_min + (b_max - b_min) * 0.5 * (1.0 + math.cos(math.pi * t / T))
    # Guard against 1.0000000002 -> 2 from floating error at t = T.
    return int(math.ceil(round(v, 9)))


def budget_table(T: int, b_min: int, b_max: int) -> list[int]:
    return [edit_budget(t, T, b_min, b_max) for t in range(T)]
