import json
import sys
from pathlib import Path

from conftest import FIX_DIFF, make_task
from rsi.verifier import run_verifier


def test_verifier_baseline_fails_tier_a(fixture_repo, heldout_dir, tmp_path):
    task = make_task(heldout_src=heldout_dir)
    reward = run_verifier(fixture_repo, task, tmp_path / "evidence")
    assert not reward.passed
    assert reward.tiers["A"]["passed"] is False
    assert reward.tiers["B"]["status"] == "skipped"
    assert reward.tiers["C"]["status"] == "skipped"
    assert reward.score == 0.0
    # Held-out files must NOT be injected when tier A fails.
    assert not (fixture_repo / "test_heldout.py").exists()


def test_verifier_passes_after_fix_and_removes_heldout(fixture_repo, heldout_dir, tmp_path):
    import subprocess
    subprocess.run(["git", "-C", str(fixture_repo), "apply",
                    "--whitespace=nowarn"], input=FIX_DIFF, text=True, check=True)
    task = make_task(heldout_src=heldout_dir)
    reward = run_verifier(fixture_repo, task, tmp_path / "evidence")
    assert reward.passed
    assert reward.score == 1.0
    assert all(t["passed"] for t in reward.tiers.values())
    # Held-out file injected for tier C, then removed again.
    assert not (fixture_repo / "test_heldout.py").exists()
    # Evidence logs exist for every executed check.
    logs = list((tmp_path / "evidence").glob("*.log"))
    assert len(logs) == 3


def test_reward_json_roundtrip(fixture_repo, heldout_dir, tmp_path):
    task = make_task(heldout_src=heldout_dir)
    reward = run_verifier(fixture_repo, task, tmp_path / "evidence")
    out = tmp_path / "reward.json"
    reward.save(out)
    data = json.loads(out.read_text())
    assert data["task_id"] == "FIX-001" and data["passed"] is False
    assert "A" in data["tiers"] and "evidence_dir" in data


def test_missing_binary_fails_check_not_silently_skipped(tmp_path):
    task = {"task_id": "X", "tiers": {
        "A": {"commands": [["definitely-not-a-real-binary-xyz"]], "timeout_s": 30}}}
    reward = run_verifier(tmp_path, task, tmp_path / "evidence")
    assert not reward.passed and reward.tiers["A"]["checks"][0]["passed"] is False


def test_cwd_dict_commands(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "marker.txt").write_text("hi")
    task = {"task_id": "X", "tiers": {
        "A": {"commands": [{"cwd": "sub", "argv": [sys.executable, "-c",
                                                   "open('marker.txt').read()"]}],
              "timeout_s": 30},
        "B": {"commands": [], "timeout_s": 30},
        "C": {"commands": [], "timeout_s": 30}}}
    reward = run_verifier(tmp_path, task, tmp_path / "evidence")
    assert reward.tiers["A"]["passed"] is True


def _go_json_script(passing: list[str], failing: list[str], rc: int) -> list:
    """A python one-liner that mimics `go test -json` output."""
    import json as _json
    lines = []
    for name in passing:
        lines.append(f"print({_json.dumps(_json.dumps({'Action': 'run', 'Test': name}))})")
        lines.append(f"print({_json.dumps(_json.dumps({'Action': 'pass', 'Test': name}))})")
    for name in failing:
        lines.append(f"print({_json.dumps(_json.dumps({'Action': 'run', 'Test': name}))})")
        lines.append(f"print({_json.dumps(_json.dumps({'Action': 'fail', 'Test': name}))})")
    lines.append(f"import sys; sys.exit({rc})")
    return [sys.executable, "-c", "\n".join(lines)]


def test_go_test_json_all_pass_mode(tmp_path):
    task = {"task_id": "X", "tiers": {
        "A": {"commands": [{"argv": _go_json_script(["T1"], ["T2"], 1),
                            "format": "go-test-json"}], "timeout_s": 30},
        "B": {"commands": [], "timeout_s": 30},
        "C": {"commands": [], "timeout_s": 30}}}
    reward = run_verifier(tmp_path, task, tmp_path / "evidence")
    check = reward.tiers["A"]["checks"][0]
    assert check["passed"] is False  # a test failed -> all-pass fails
    assert check["tests"] == {"T1": True, "T2": False}
    assert reward.tiers["B"]["status"] == "skipped"  # fail-fast


def test_go_test_json_suite_ran_mode(tmp_path):
    task = {"task_id": "X", "tiers": {
        "A": {"commands": [{"argv": _go_json_script(["T1"], [], 0),
                            "format": "go-test-json"}], "timeout_s": 30},
        "B": {"commands": [{"argv": _go_json_script(["T1"], ["T2"], 1),
                            "format": "go-test-json"}],
              "timeout_s": 30, "mode": "suite-ran"},
        "C": {"commands": [{"argv": _go_json_script(["T1"], [], 0),
                            "format": "go-test-json"}], "timeout_s": 30}}}
    reward = run_verifier(tmp_path, task, tmp_path / "evidence")
    assert reward.tiers["B"]["passed"] is True  # suite ran; failures are per-test
    assert reward.tiers["B"]["checks"][0]["tests"] == {"T1": True, "T2": False}
    assert reward.passed is True


def test_go_test_json_no_events_means_suite_did_not_run(tmp_path):
    task = {"task_id": "X", "tiers": {
        "A": {"commands": [{"argv": [sys.executable, "-c", "import sys; sys.exit(1)"],
                            "format": "go-test-json"}], "timeout_s": 30},
        "B": {"commands": [], "timeout_s": 30},
        "C": {"commands": [], "timeout_s": 30}}}
    reward = run_verifier(tmp_path, task, tmp_path / "evidence")
    assert reward.tiers["A"]["checks"][0]["passed"] is False
    assert reward.tiers["A"]["checks"][0]["tests"] == {}


def test_baseline_runs_all_tiers_when_not_fail_fast(fixture_repo, heldout_dir, tmp_path):
    task = make_task(heldout_src=heldout_dir)
    reward = run_verifier(fixture_repo, task, tmp_path / "evidence",
                          fail_fast=False)
    assert reward.tiers["A"]["passed"] is False  # bug reproduces
    assert reward.tiers["B"]["status"] == "run"  # still ran (P2P map)
    assert reward.tiers["C"]["status"] == "run"  # held-out baseline captured


def _repo_without_repro(tmp_path, name):
    """Fixture repo with the calc bug but no committed repro test."""
    from conftest import CALC_BUG, OPS, TEST_OTHER, _git

    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "rsi-test@example.invalid")
    _git(repo, "config", "user.name", "rsi-test")
    (repo / "calc.py").write_text(CALC_BUG)
    (repo / "ops.py").write_text(OPS)
    (repo / "test_other.py").write_text(TEST_OTHER)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "buggy, no repro test committed")
    return repo


def test_tier_a_repro_injected_then_removed(tmp_path):
    """A repro test that does not exist in the repo is injected for tier A
    (verifier-owned, Fixer never sees it) and removed right after, so tier B
    measures only pre-existing tests. CAND-001 pilot gap."""
    from conftest import TEST_CALC, make_task

    repo = _repo_without_repro(tmp_path, "repo-norepro")
    repro_dir = tmp_path / "repro"
    repro_dir.mkdir()
    (repro_dir / "test_calc.py").write_text(TEST_CALC)
    task = make_task()
    task["tiers"]["A"]["repro"] = {"src": str(repro_dir), "dest": "."}
    task["tiers"]["C"]["commands"] = [[sys.executable, "-m", "pytest", "-q",
                                       "test_other.py"]]
    reward = run_verifier(repo, task, tmp_path / "evidence", fail_fast=False)
    assert reward.tiers["A"]["passed"] is False  # bug reproduces via injected repro
    assert not (repo / "test_calc.py").exists()  # removed after tier A
    assert reward.tiers["B"]["passed"] is True  # only pre-existing tests
    assert reward.tiers["C"]["passed"] is True


def test_tier_a_repro_passes_after_fix(tmp_path):
    """Same setup, but the fix is applied first: all tiers pass."""
    import subprocess

    from conftest import FIX_DIFF, TEST_CALC, make_task

    repo = _repo_without_repro(tmp_path, "repo-norepro-fixed")
    subprocess.run(["git", "-C", str(repo), "apply", "--whitespace=nowarn"],
                   input=FIX_DIFF, text=True, check=True)
    repro_dir = tmp_path / "repro"
    repro_dir.mkdir()
    (repro_dir / "test_calc.py").write_text(TEST_CALC)
    task = make_task()
    task["tiers"]["A"]["repro"] = {"src": str(repro_dir), "dest": "."}
    task["tiers"]["C"]["commands"] = [[sys.executable, "-m", "pytest", "-q",
                                       "test_other.py"]]
    reward = run_verifier(repo, task, tmp_path / "evidence")
    assert reward.passed is True
    assert reward.score == 1.0
    assert not (repo / "test_calc.py").exists()
