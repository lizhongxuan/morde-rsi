"""Algorithm 2 of the RRSI paper, the selection side, as pure functions.

For every screened candidate with measurement (S', C'):

    Delta S = S' - S_t,   Delta C = (C' - C_t) / C_t
    c = [Delta C <= beta0 + beta1 Delta S]             if Delta S > delta
        [w_s Delta S - w_c Delta C + w_n nu > 0]       otherwise
    admissible  iff  S' >= S* - delta  and  c  (and every domain guard holds)

    winner = argmax_{admissible} S',  or incumbent if none is admissible

Borrowed from RRSI rrsi/selection.py (cost_rule, judge, select_round,
Decision, relative_cost_change). Changed:

* S is the weighted pass rate of the task's checks, produced by the morde
  domain's score(): F2P (repro test) weight 2, P2P (regression) weight 1,
  held-out weight 2. RRSI's S was the fraction of benchmark trials passing.
* One extra HARD GATE, before the floor: every F2P check must pass and no
  P2P check may regress. RRSI has no such semantic gate; for a bug fix it
  is the load-bearing condition (a candidate that does not fix the bug, or
  breaks something else, is never admissible however the aggregate looks).
* novelty() is kept for the within-band shaped rule; component counts come
  from the ledger (accepted edits per component).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class TaskResult:
    rewards: list[float]                  # one per check; missing -> 0.0
    weights: list[float] = field(default_factory=list)
    tokens: list = field(default_factory=list)
    missing: int = 0
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.weights:
            self.weights = [1.0] * len(self.rewards)
        if not self.tokens:
            self.tokens = [None] * len(self.rewards)

    @property
    def mean(self) -> float:
        w = sum(self.weights)
        return (sum(r * x for r, x in zip(self.rewards, self.weights)) / w) if w else 0.0


@dataclass
class EvalResult:
    job: str
    k: int
    per_task: dict                        # check-group -> TaskResult
    S: float                              # weighted pass rate
    C: float | None                       # cost (tokens or seconds)
    n_expected: int
    missing: int
    f2p_pass: bool = True                 # every fail-to-pass check passes
    p2p_clean: bool = True                # no pass-to-pass check regressed
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        d = {"job": self.job, "k": self.k, "S": self.S, "C": self.C,
             "n_expected": self.n_expected, "missing": self.missing,
             "f2p_pass": self.f2p_pass, "p2p_clean": self.p2p_clean,
             "extra": self.extra,
             "per_task": {t: asdict(r) for t, r in self.per_task.items()}}
        return d

    @classmethod
    def from_json(cls, d: dict) -> "EvalResult":
        per = {t: TaskResult(**r) for t, r in d["per_task"].items()}
        return cls(job=d["job"], k=d["k"], per_task=per, S=d["S"], C=d["C"],
                   n_expected=d["n_expected"], missing=d["missing"],
                   f2p_pass=d.get("f2p_pass", True),
                   p2p_clean=d.get("p2p_clean", True),
                   extra=d.get("extra") or {})


def aggregate(job: str, k: int, per_task: dict, extra: dict | None = None,
              f2p_pass: bool = True, p2p_clean: bool = True) -> EvalResult:
    """Fold per-group check records into S (weighted pass rate) and C."""
    num = den = 0.0
    toks = []
    missing = 0
    for tr in per_task.values():
        for r, w in zip(tr.rewards, tr.weights):
            num += r * w
            den += w
        toks += [x for x in tr.tokens if isinstance(x, (int, float)) and x > 0]
        missing += tr.missing
    return EvalResult(job=job, k=k, per_task=per_task,
                      S=(num / den) if den else 0.0,
                      C=(sum(toks) / len(toks)) if toks else None,
                      n_expected=len(per_task) * k, missing=missing,
                      f2p_pass=f2p_pass, p2p_clean=p2p_clean,
                      extra=dict(extra or {}))


def relative_cost_change(C_cand: float | None, C_inc: float | None) -> float:
    """Delta C = (C' - C_t) / C_t; 0 when either side has no cost count."""
    if not C_cand or not C_inc:
        return 0.0
    return (C_cand - C_inc) / C_inc


# Check-group weights for the morde score (see judge's hard gate below).
GROUP_WEIGHTS = {"A": 2.0, "B": 1.0, "C": 2.0}
GROUP_NAMES = {"A": "f2p", "B": "p2p", "C": "heldout"}


def test_map(reward, tier_name: str) -> dict:
    """Merged per-test results of one tier: {test name: passed}."""
    tiers = reward.tiers if hasattr(reward, "tiers") else reward.get("tiers", {})
    tier = tiers.get(tier_name, {})
    out: dict[str, bool] = {}
    for c in tier.get("checks", []) if isinstance(tier, dict) else []:
        out.update(c.get("tests", {}) or {})
    return out


def eval_from_reward(task_id: str, reward, job: str,
                     f2p_pass: bool | None = None,
                     p2p_clean: bool | None = None) -> EvalResult:
    """Map a verifier Reward (or its dict form) to RRSI's EvalResult.

    S = weighted pass rate over the task's checks. Per tier, when the checks
    carry per-test results (go-test-json), the tier is scored per test;
    otherwise it is scored per command. Weights: tier A (repro /
    fail-to-pass) 2, tier B (regression) 1, tier C (held-out) 2.

    F2P passes iff tier A passed; P2P is clean iff tier B passed. The caller
    (controller) may override both with per-test comparisons against the
    baseline map: a test that passed at baseline must still pass after the
    fix; a test that already failed at baseline is not part of P2P.
    """
    tiers = reward.tiers if hasattr(reward, "tiers") else reward.get("tiers", {})
    per = {}
    for name, tier in tiers.items():
        checks = tier.get("checks", []) if isinstance(tier, dict) else []
        if tier.get("status") == "skipped" or not checks:
            continue
        test_results: dict[str, bool] = {}
        for c in checks:
            test_results.update(c.get("tests", {}) or {})
        if test_results:
            rewards = [1.0 if v else 0.0 for v in test_results.values()]
            weights = [GROUP_WEIGHTS.get(name, 1.0)] * len(rewards)
        else:
            rewards = [1.0 if c["passed"] else 0.0 for c in checks]
            weights = [GROUP_WEIGHTS.get(name, 1.0)] * len(checks)
        per[GROUP_NAMES.get(name, name)] = TaskResult(
            rewards=rewards, weights=weights,
            tokens=[None] * len(rewards))
    if f2p_pass is None:
        f2p_pass = tiers.get("A", {}).get("passed", False)
    if p2p_clean is None:
        p2p_clean = tiers.get("B", {}).get("passed", True)
    return aggregate(job, 1, per, f2p_pass=bool(f2p_pass),
                     p2p_clean=bool(p2p_clean))


def novelty(components: list[str], incumbent_counts: dict) -> int:
    """nu: number of structural component types never in a winning edit."""
    return sum(1 for c in set(components or [])
               if not incumbent_counts.get(c))


@dataclass
class Candidate:
    variant: str
    edits: list[dict]                     # each tagged (component, hypothesis)
    ev: EvalResult | None = None
    diff_path: str | None = None
    branch: str | None = None
    commit: str | None = None
    gate_failure: str | None = None       # critic_reject / smoke_fail / eval_invalid
    detail: str = ""

    @property
    def components(self) -> list[str]:
        return [e.get("component") for e in self.edits if e.get("component")]


@dataclass
class Decision:
    variant: str
    admissible: bool
    reason: str
    S: float | None = None
    C: float | None = None
    delta_S: float | None = None
    delta_C: float | None = None
    novelty: int = 0
    guards: list = field(default_factory=list)

    def to_json(self) -> dict:
        return self.__dict__


def cost_rule(delta_S: float, delta_C: float, nov: int, delta: float,
              cfg) -> tuple[bool, str]:
    """c of Algorithm 2, line 5 (RRSI selection.py, unchanged)."""
    if delta_S > delta:
        budget = cfg.beta0 + cfg.beta1 * delta_S
        ok = delta_C <= budget
        return ok, (f"gain {delta_S:+.4f} > delta {delta:.4f}; cost change "
                    f"{delta_C:+.3f} {'<=' if ok else '>'} budget {budget:.3f} "
                    f"(beta0 {cfg.beta0} + beta1 {cfg.beta1} * dS)")
    shaped = cfg.w_s * delta_S - cfg.w_c * delta_C + cfg.w_n * nov
    ok = shaped > 0
    return ok, (f"gain {delta_S:+.4f} within delta {delta:.4f}; shaped "
                f"{cfg.w_s}*dS - {cfg.w_c}*dC + {cfg.w_n}*nu = {shaped:+.4f} "
                f"{'>' if ok else '<='} 0 (nu={nov})")


def classify_p2p_failure(base_map: dict, cand_map: dict, *,
                         tier_a_pass: bool, tier_c_pass: bool,
                         related_tests: set[str] | frozenset | None = None) -> dict:
    """Classify a pass-to-pass failure: clean, clear regression, or ambiguous.

    CAND-001 lesson: the P2P gate catches a behaviour change but cannot tell
    "a test encoded the buggy behaviour" from "a real regression". That call
    belongs to a human, so a *single* newly-failing test with tiers A and C
    both green is routed to NEEDS_HUMAN instead of auto-REJECT.

    * clean: no test that passed at baseline now fails.
    * ambiguous_single: exactly one newly-failing test, tier A and tier C
      both passed, and (when the caller supplies `related_tests` -- the
      tests asserting the changed behaviour) the failing test is one of
      them. When `related_tests` is not supplied the single failure is
      still ambiguous: file proximity does not capture a behavioural
      relation (in CAND-001 the failing test lived in a different file
      from the fix), and asking a human about one test is cheap while
      wrongly rejecting a correct fix is expensive.
    * clear_regression: everything else (A or C failed, two or more newly
      failing tests, or the single failure is unrelated to the change).

    The Fixer must never resolve this by editing the test: changing a test
    expectation is a human edit, outside the loop (critic: GAMING-DETECTED).
    """
    failed = [t for t, ok in base_map.items()
              if ok and not cand_map.get(t, False)]
    if not failed:
        return {"kind": "clean", "failed": [],
                "reason": "no pass-to-pass failure"}
    if not tier_a_pass or not tier_c_pass:
        return {"kind": "clear_regression", "failed": sorted(failed),
                "reason": "tier A or tier C failed; not a lone P2P question"}
    if len(failed) == 1:
        if related_tests is not None and failed[0] not in related_tests:
            return {"kind": "clear_regression", "failed": sorted(failed),
                    "reason": (f"single failure {failed[0]!r} is unrelated "
                               "to the changed behaviour")}
        why = (f"single newly-failing test {failed[0]!r} with tiers A and C "
               "green: test may encode the buggy behaviour")
        if related_tests is None:
            why += " (relatedness undeclared; routed conservatively)"
        return {"kind": "ambiguous_single", "failed": sorted(failed),
                "reason": why}
    return {"kind": "clear_regression", "failed": sorted(failed),
            "reason": f"{len(failed)} newly-failing tests, not a single point"}


def judge(cand: Candidate, incumbent: EvalResult, S_star: float, delta: float,
          cfg, incumbent_counts: dict, guards: list[str] | None = None) -> Decision:
    ev = cand.ev
    if ev is None:
        return Decision(cand.variant, False, cand.gate_failure or "not evaluated")
    dS = ev.S - incumbent.S
    dC = relative_cost_change(ev.C, incumbent.C)
    nov = novelty(cand.components, incumbent_counts)
    d = Decision(cand.variant, False, "", S=ev.S, C=ev.C, delta_S=dS, delta_C=dC,
                 novelty=nov, guards=list(guards or []))
    # morde-rsi hard gate (not in RRSI): the bug must actually be fixed and
    # nothing that passed before may regress.
    if not ev.f2p_pass:
        d.reason = "hard gate failed: fail-to-pass check did not pass"
        return d
    if not ev.p2p_clean:
        d.reason = "hard gate failed: pass-to-pass regression"
        return d
    floor = S_star - delta
    if ev.S < floor:
        d.reason = (f"below noise-adjusted floor: S' {ev.S:.4f} < S* {S_star:.4f} "
                    f"- delta {delta:.4f}")
        return d
    ok, why = cost_rule(dS, dC, nov, delta, cfg)
    if not ok:
        d.reason = f"cost rule failed: {why}"
        return d
    if guards:
        d.reason = "domain guard violated: " + "; ".join(guards)
        return d
    d.admissible = True
    d.reason = f"admissible: {why}"
    return d


def select_round(cands: list[Candidate], incumbent: EvalResult, S_star: float,
                 delta: float, cfg, incumbent_counts: dict,
                 guard_fn=None) -> tuple[Candidate | None, list[Decision]]:
    """Judge every candidate, return (winner or None, decisions)."""
    decisions = []
    for c in cands:
        g = guard_fn(incumbent, c.ev) if (guard_fn and c.ev is not None) else []
        decisions.append(judge(c, incumbent, S_star, delta, cfg,
                               incumbent_counts, g))
    adm = [(c, d) for c, d in zip(cands, decisions) if d.admissible]
    if not adm:
        return None, decisions
    winner, _ = max(adm, key=lambda cd: cd[1].S)
    return winner, decisions
