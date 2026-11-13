"""Run configuration, mirroring RRSI's RRSIConfig shape (rrsi/config.py).

Loaded from domains/<domain>/rrsi.json; CLI flags override individual keys.
Unknown keys in the JSON (e.g. "_doc") are ignored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RSIConfig:
    # RRSI hyperparameters (same key names as domains/coding/rrsi.json).
    T: int = 5
    k: int = 1
    m: int = 1
    b_min: int = 1
    b_max: int = 2
    w: int = 3
    m_draft: int = 1
    delta: float = 0.02
    delta_z: float = 2.0
    beta0: float = 0.10
    beta1: float = 44.5
    w_s: float = 0.0
    w_c: float = 15.0
    w_n: float = 0.5
    n_prune: int = 4
    repair_rounds: int = 3
    # morde-rsi additions: hard budgets for the fixer.
    max_submissions: int = 3
    token_budget: int | None = None
    time_budget_s: int = 3600
    timeout_s: int = 600
    # domain-level settings (from domains/<domain>/rrsi.json).
    repo_path: str = ""
    fixer: str = "dsh"
    fixer_model: str = "deepseek-v4-flash"

    @classmethod
    def load(cls, path: Path | str, **overrides) -> "RSIConfig":
        data = {}
        if path is not None:
            data = json.loads(Path(path).read_text())
        known = {f.name for f in cls.__dataclass_fields__.values()}
        data = {k: v for k, v in data.items() if k in known}
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)

    def to_json(self) -> dict:
        return {f.name: getattr(self, f.name)
                for f in self.__dataclass_fields__.values()}
