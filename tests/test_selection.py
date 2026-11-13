from rsi.config import RSIConfig
from rsi.selection import (Candidate, EvalResult, TaskResult, aggregate,
                           cost_rule, judge, relative_cost_change,
                           select_round)

CFG = RSIConfig.load(None)


def _ev(s, c=None, f2p=True, p2p=True):
    return EvalResult(job="t", k=1, per_task={}, S=s, C=c, n_expected=1,
                      missing=0, f2p_pass=f2p, p2p_clean=p2p)


def test_aggregate_missing_counts_zero():
    ev = aggregate("t", 1, {"f2p": TaskResult(rewards=[1.0], weights=[2.0]),
                            "p2p": TaskResult(rewards=[0.0], weights=[1.0])})
    assert ev.S == 2.0 / 3.0


def test_cost_rule_above_delta():
    ok, _ = cost_rule(0.5, 0.05, 0, 0.02, CFG)
    assert ok  # 0.05 <= 0.1 + 44.5*0.5
    ok, _ = cost_rule(0.5, 30.0, 0, 0.02, CFG)
    assert not ok


def test_judge_hard_gate_f2p():
    d = judge(Candidate("A", []), _ev(0.0), 0.0, 0.02, CFG, {},
              guards=None)
    # candidate below has no eval -> not evaluated
    assert not d.admissible
    cand = Candidate("A", [], ev=_ev(1.0, f2p=False))
    d = judge(cand, _ev(0.0), 0.0, 0.02, CFG, {})
    assert not d.admissible and "fail-to-pass" in d.reason


def test_judge_hard_gate_p2p():
    cand = Candidate("A", [], ev=_ev(1.0, p2p=False))
    d = judge(cand, _ev(0.0), 0.0, 0.02, CFG, {})
    assert not d.admissible and "regression" in d.reason


def test_judge_floor_blocks_within_band_loss():
    cand = Candidate("A", [], ev=_ev(0.90))
    d = judge(cand, _ev(1.0), 1.0, 0.02, CFG, {})
    assert not d.admissible and "floor" in d.reason


def test_judge_admits_real_gain():
    cand = Candidate("A", [{"component": "logic-small"}], ev=_ev(1.0))
    d = judge(cand, _ev(0.0), 0.0, 0.02, CFG, {})
    assert d.admissible


def test_select_round_argmax():
    cands = [Candidate("A", [], ev=_ev(0.8)),
             Candidate("B", [], ev=_ev(1.0))]
    winner, decisions = select_round(cands, _ev(0.0), 0.0, 0.02, CFG, {})
    assert winner is not None and winner.variant == "B"
    assert all(d.admissible for d in decisions)
    winner, _ = select_round([Candidate("A", [], ev=_ev(0.0))],
                             _ev(1.0), 1.0, 0.02, CFG, {})
    assert winner is None


def test_relative_cost_change_no_tokens_is_zero():
    assert relative_cost_change(None, 100.0) == 0.0
    assert relative_cost_change(50.0, None) == 0.0
    assert relative_cost_change(150.0, 100.0) == 0.5


def test_eval_from_reward_per_test_groups():
    import pytest

    from rsi.selection import eval_from_reward, test_map
    reward = {"tiers": {
        "A": {"passed": True, "status": "run", "checks": [
            {"passed": True, "tests": {"TestRepro": True}}]},
        "B": {"passed": False, "status": "run", "checks": [
            {"passed": True, "tests": {"TestKeep": True, "TestEnv": False}}]},
        "C": {"passed": True, "status": "run", "checks": [
            {"passed": True, "tests": {"TestHeld": True}}]},
    }}
    ev = eval_from_reward("T", reward, "candidate")
    # f2p (A, w2): 1.0; p2p (B, w1): 1/2; heldout (C, w2): 1.0
    # S = (2*1 + 1*1 + 1*0 + 2*1) / (2+1+1+2) = 5/6
    assert ev.S == pytest.approx(5 / 6)
    assert ev.f2p_pass is True
    assert ev.p2p_clean is False  # tier-B flag fallback (no override)
    assert test_map(reward, "B") == {"TestKeep": True, "TestEnv": False}
    ev2 = eval_from_reward("T", reward, "candidate", p2p_clean=True)
    assert ev2.p2p_clean is True


def test_eval_from_reward_falls_back_to_commands():
    from rsi.selection import eval_from_reward
    reward = {"tiers": {
        "A": {"passed": True, "status": "run", "checks": [{"passed": True, "tests": {}}]},
        "B": {"passed": True, "status": "run", "checks": [{"passed": True, "tests": {}}]},
    }}
    ev = eval_from_reward("T", reward, "candidate")
    assert ev.S == 1.0 and ev.f2p_pass and ev.p2p_clean

# ------------------------------------------------------------- k-trial -----

def test_eval_from_reward_k2_trials_mean():
    """A check that ran 2 trials (1 fail, 1 pass) contributes two rewards,
    so S is 0.5 -- the trial mean, not the strict check flag."""
    from rsi.selection import eval_from_reward
    reward = {"tiers": {
        "A": {"passed": False, "status": "run", "checks": [
            {"passed": False, "tests": {},
             "trials": [{"passed": False, "returncode": 1, "missing": False},
                        {"passed": True, "returncode": 0, "missing": False}]}]},
    }}
    ev = eval_from_reward("T", reward, "job", k=2)
    assert ev.S == 0.5
    assert ev.k == 2 and ev.n_expected == 2
    assert ev.missing == 0


def test_eval_from_reward_missing_trial_counts_zero():
    """A timed-out trial (returncode None, missing=True) contributes 0
    with the denominator kept: rewards [0, 1] over 2 trials -> S 0.5,
    missing 1, n_expected 2 (not 1)."""
    from rsi.selection import eval_from_reward
    reward = {"tiers": {
        "A": {"passed": False, "status": "run", "checks": [
            {"passed": False, "tests": {},
             "trials": [{"passed": False, "returncode": None, "missing": True},
                        {"passed": True, "returncode": 0, "missing": False}]}]},
    }}
    ev = eval_from_reward("T", reward, "job", k=2)
    assert ev.S == 0.5
    assert ev.missing == 1
    assert ev.n_expected == 2


def test_eval_from_reward_k1_unchanged():
    """No trials recorded (old reward.json shape) and k=1: exactly the
    old behaviour -- one reward per check, k recorded as 1."""
    from rsi.selection import eval_from_reward
    reward = {"tiers": {
        "A": {"passed": True, "status": "run", "checks": [
            {"passed": True, "tests": {}}]},
        "B": {"passed": True, "status": "run", "checks": [
            {"passed": False, "tests": {}}]},
    }}
    ev = eval_from_reward("T", reward, "job")
    # f2p (A, w2): 1.0; p2p (B, w1): 0.0 -> S = 2/3 (exact float division)
    assert ev.S == 2 / 3
    assert ev.k == 1 and ev.missing == 0


def test_eval_from_reward_per_test_scored_still_counts_missing():
    """On the per-test path (go-test-json) the per-test rewards dominate,
    but a missing trial is still counted: it ran and failed, even though
    it produced no test results."""
    from rsi.selection import eval_from_reward
    reward = {"tiers": {
        "B": {"passed": True, "status": "run", "checks": [
            {"passed": True, "tests": {"TestKeep": True, "TestEnv": False},
             "trials": [{"passed": False, "returncode": None, "missing": True},
                        {"passed": True, "returncode": 1, "missing": False}]}]},
    }}
    ev = eval_from_reward("T", reward, "job", k=2)
    assert ev.S == 0.5  # per-test: 1 of 2 tests passed
    assert ev.missing == 1
