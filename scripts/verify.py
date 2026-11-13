#!/usr/bin/env python3
"""Deterministic verifier CLI (the Judge).

    python scripts/verify.py --worktree <path> --task <task.json> --out reward.json

The task JSON carries the tier commands; tier C's held-out tests are injected
from --heldout-dir (outside the worktree, never visible to the Fixer) and
removed after the run. Exits 0 iff every executed tier passed.

This is a script, not an agent. It never calls a model.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi.verifier import run_verifier


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--task", required=True,
                    help="task JSON file (tiers A/B/C with commands)")
    ap.add_argument("--out", required=True, help="reward.json output path")
    ap.add_argument("--evidence-dir", default=None)
    ap.add_argument("--heldout-dir", default=None,
                    help="directory holding held-out test files for tier C")
    args = ap.parse_args()

    task = json.loads(Path(args.task).read_text())
    if args.heldout_dir:
        c_cfg = task.setdefault("tiers", {}).setdefault("C", {})
        c_cfg.setdefault("heldout", {})["src"] = args.heldout_dir
        c_cfg["heldout"].setdefault("dest", "heldout")
    evidence_dir = (Path(args.evidence_dir) if args.evidence_dir
                    else Path(args.out).parent / "evidence")
    reward = run_verifier(args.worktree, task, evidence_dir,
                          heldout_base=Path(args.task).parent)
    reward.save(args.out)
    print(json.dumps(reward.to_json(), indent=1, ensure_ascii=False))
    return 0 if reward.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
