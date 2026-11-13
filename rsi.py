#!/usr/bin/env python3
"""morde-rsi command line, mirroring RRSI rrsi.py's subcommand shape.

    python rsi.py --domain morde smoke      # liveness: go/node/verifier present
    python rsi.py --domain morde baseline   # evaluate the baseline, seed frontier
    python rsi.py --domain morde round --t 0 [--fixer manual --diff <file>]
    python rsi.py --domain morde run        # rounds until T / tasks done / STOP
    python rsi.py --domain morde status     # frontier + recent history

RRSI's calibrate / readjudicate / reevaluate / heldout are deliberately NOT
here: the pilot does not need them. The DSH fixer is a stub until wired;
--fixer manual runs the pilot/manual mode (SIMULATED, labelled in the ledger).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rsi.config import RSIConfig
from rsi.controller import Controller
from rsi.feedback import FEEDBACK_KINDS, FEEDBACK_LABELS, FeedbackLog, FeedbackRecord
from rsi.fixer import DSHFixer, ManualFixer


def load_domain(name: str):
    sys.path.insert(0, str(ROOT / "domains" / name))
    import adapter  # noqa: E402
    return adapter.DOMAIN


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", default="morde")
    ap.add_argument("--repo", default=None,
                    help="target repo path (default: domains/morde/rrsi.json repo_path)")
    ap.add_argument("--runs", default=None,
                    help="runs dir (default: runs/<domain>)")
    ap.add_argument("--fixer", choices=["dsh", "manual"], default="manual")
    ap.add_argument("--diff", default=None,
                    help="diff file for --fixer manual")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("smoke")
    sub.add_parser("baseline")
    p = sub.add_parser("round")
    p.add_argument("--t", type=int, required=True)
    sub.add_parser("run").add_argument("--start", type=int, default=0)
    sub.add_parser("status")
    p = sub.add_parser("feedback")
    p.add_argument("--task", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--kind", choices=sorted(FEEDBACK_KINDS),
                   default="human_label")
    p.add_argument("--label", choices=sorted(FEEDBACK_LABELS), default=None)
    p.add_argument("--note", required=True,
                   help="one sentence: what happened / what you decided")
    p.add_argument("--round", type=int, default=None,
                   help="round the feedback belongs to (needed for a "
                        "human_resolution to requeue a NEEDS_HUMAN task)")
    args = ap.parse_args()

    domain = load_domain(args.domain)
    cfg = RSIConfig.load(domain.root / "rrsi.json")
    runs_dir = Path(args.runs) if args.runs else ROOT / "runs" / args.domain
    repo = Path(args.repo) if args.repo else Path(cfg.repo_path or "")
    if args.cmd == "feedback":
        # Record human feedback by hand (answering an approval card, or
        # resolving a NEEDS_HUMAN). Needs no repo and no fixer.
        # A human_resolution requeues the task on the next run/round, e.g.:
        #   --kind human_resolution --round 0
        #   --note "update-test-expectation: Projects.test.tsx:355 now
        #          expects '已分派任务'"
        if args.kind == "human_label" and not args.label:
            sys.exit("--kind human_label needs --label")
        log = FeedbackLog(runs_dir / "human_feedback.jsonl")
        rec = log.append(FeedbackRecord(
            candidate_id=args.candidate, task_id=args.task, kind=args.kind,
            note=args.note, source="human", label=args.label,
            round_no=args.round))
        print(json.dumps(rec, indent=1, ensure_ascii=False))
        return 0
    if not repo.exists():
        sys.exit(f"repo not found: {repo} (set --repo or rrsi.json repo_path)")

    if args.cmd == "smoke":
        ok, detail = domain.smoke(repo)
        print(json.dumps({"ok": ok, **detail}, indent=1, ensure_ascii=False))
        return 0 if ok else 1

    # status needs no fixer at all.
    if args.cmd == "status":
        ctl = Controller(repo, runs_dir,
                         domain.root / "tasks" / "evolve.json", cfg, None)
        print(json.dumps(ctl.status(), indent=1, ensure_ascii=False))
        return 0

    if args.fixer == "manual":
        if not args.diff:
            sys.exit("--fixer manual needs --diff <diff file>")
        fixer = ManualFixer(args.diff)
    else:
        fixer = DSHFixer(skill_md=(domain.root / "SKILL.md").read_text()
                         if (domain.root / "SKILL.md").exists() else "")
    ctl = Controller(repo, runs_dir,
                     domain.root / "tasks" / "evolve.json", cfg, fixer,
                     skill_md=(domain.root / "SKILL.md").read_text()
                     if (domain.root / "SKILL.md").exists() else "",
                     guard_fn=getattr(domain, "guards_for_diff", None))

    if args.cmd == "baseline":
        task = ctl.pick_next_task()
        if task is None:
            sys.exit("no tasks in evolve.json")
        print(json.dumps(ctl.run_task(task, 0), indent=1, ensure_ascii=False))
        return 0
    if args.cmd == "round":
        task = ctl.pick_next_task()
        if task is None:
            sys.exit("no more tasks")
        print(json.dumps(ctl.run_task(task, args.t), indent=1,
                         ensure_ascii=False))
        return 0
    if args.cmd == "run":
        print(json.dumps(ctl.run(args.start), indent=1, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
