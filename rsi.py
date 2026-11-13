#!/usr/bin/env python3
"""morde-rsi command line, mirroring RRSI rrsi.py's subcommand shape.

    python rsi.py --domain morde smoke      # liveness: go/node/verifier present
    python rsi.py --domain morde baseline   # evaluate the baseline, seed frontier
    python rsi.py --domain morde calibrate [--reps 2000]
                                            # re-estimate delta from repeated
                                            # baseline evaluations in runs/<domain>/
    python rsi.py --domain morde round --t 0 [--fixer manual --diff <file>]
    python rsi.py --domain morde run        # rounds until T / tasks done / STOP
    python rsi.py --domain morde status     # frontier + recent history

RRSI's readjudicate / reevaluate / heldout are deliberately NOT here: the
pilot does not need them. `calibrate` was cut in the pilot (delta was a fixed
0.02 placeholder) and is now wired back: with rrsi.json's delta null
("unmeasured"), the CLI adopts runs/<domain>/calibration.json when it exists
and tries to re-estimate it after a baseline run. The DSH fixer is a stub
until wired; --fixer manual runs the pilot/manual mode (SIMULATED, labelled
in the ledger).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rsi.calibrate import auto_calibrate, read_calibration
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
    p = sub.add_parser("calibrate")
    p.add_argument("--reps", type=int, default=2000,
                   help="bootstrap replicates (default 2000; seed fixed at 7)")
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
    if args.cmd == "calibrate":
        # Needs no repo and no fixer: it only reads baseline evaluations
        # already saved under runs/<domain>/ (baseline-eval-*.json /
        # baseline-reward-*.json). No usable data -> fail loudly, never
        # invent a delta.
        try:
            cal, path = auto_calibrate(runs_dir, z=cfg.delta_z,
                                       reps=args.reps)
        except ValueError as e:
            sys.exit(f"calibrate: {e}")
        print(json.dumps(cal, indent=1, ensure_ascii=False))
        print(f"delta = {cal['delta']} ({cal['method']}; "
              f"n_evals={cal['n_evals']}) -> {path}")
        return 0

    # delta=null in rrsi.json means "unmeasured": a measured value in
    # runs/<domain>/calibration.json (written by `calibrate`) wins over
    # failing in judge(). This runs before the Controller is built, so the
    # adopted value is what judge() sees.
    delta_was_null = cfg.delta is None
    if delta_was_null and args.cmd in ("baseline", "round", "run", "status"):
        cal_delta = read_calibration(runs_dir)
        if cal_delta is not None:
            cfg.delta = cal_delta
            print(f"note: delta is null in rrsi.json; using measured "
                  f"delta={cal_delta} from {runs_dir / 'calibration.json'}",
                  file=sys.stderr)
        elif args.cmd in ("baseline", "round", "run"):
            print("warning: delta is unmeasured (rrsi.json delta=null and "
                  "no calibration.json); judge needs a delta -- run "
                  "`rsi.py --domain morde calibrate` after collecting "
                  "repeated baseline evaluations, or set delta in rrsi.json",
                  file=sys.stderr)
    if not repo.exists():
        sys.exit(f"repo not found: {repo} (set --repo or rrsi.json repo_path)")

    if args.cmd == "smoke":
        ok, detail = domain.smoke(repo)
        print(json.dumps({"ok": ok, **detail}, indent=1, ensure_ascii=False))
        return 0 if ok else 1

    # status needs no fixer at all.
    if args.cmd == "status":
        ctl = Controller(repo, runs_dir,
                         domain.root / "tasks" / "evolve.json", cfg, None,
                         domain=args.domain,
                         domain_patterns=getattr(domain, "critic_patterns",
                                                 None),
                         domain_components=getattr(domain, "components",
                                                   None))
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
                     guard_fn=getattr(domain, "guards_for_diff", None),
                     domain=args.domain,
                     domain_patterns=getattr(domain, "critic_patterns",
                                             None),
                     worktree_excludes=getattr(
                         domain, "fixer_worktree_excludes", None),
                     domain_components=getattr(domain, "components",
                                               None))

    if args.cmd == "baseline":
        task = ctl.pick_next_task()
        if task is None:
            sys.exit("no tasks in evolve.json")
        result = ctl.run_task(task, 0)
        if delta_was_null:
            # Baseline done: if repeated baseline evaluations are on disk,
            # re-estimate delta now and persist it for the next rounds.
            # No data -> say so on stderr and keep delta unmeasured.
            try:
                cal, path = auto_calibrate(runs_dir, z=cfg.delta_z)
            except ValueError as e:
                print(f"note: delta stays unmeasured: {e}", file=sys.stderr)
            else:
                result["calibration"] = {"delta": cal["delta"],
                                         "path": str(path),
                                         "method": cal["method"]}
                print(f"auto-calibrated delta = {cal['delta']} "
                      f"({cal['method']}; n_evals={cal['n_evals']}) -> {path}")
        print(json.dumps(result, indent=1, ensure_ascii=False))
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
