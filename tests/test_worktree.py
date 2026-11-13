import subprocess

from conftest import FIX_DIFF
from rsi import worktree as wt


def test_create_apply_commit_remove(fixture_repo, tmp_path):
    repo = fixture_repo
    base = wt.head_commit(repo)
    path = tmp_path / "wt1"
    wt.create_worktree(repo, path, "rsi/test-1", start_point=base)
    assert (path / "calc.py").exists()
    wt.apply_diff(path, FIX_DIFF)
    assert "a + b\n" in (path / "calc.py").read_text()
    diff = wt.diff_of_worktree(path, base)
    assert "return a + b" in diff
    commit = wt.commit_worktree(path, "rsi: test fix")
    assert commit != base
    wt.remove_worktree(repo, path, "rsi/test-1")
    assert not path.exists()
    names = [w["path"] for w in wt.list_worktrees(repo)]
    assert str(path) not in names


def test_apply_bad_diff_raises(fixture_repo, tmp_path):
    repo = fixture_repo
    path = tmp_path / "wt2"
    wt.create_worktree(repo, path, "rsi/test-2")
    try:
        import pytest
        with pytest.raises(RuntimeError):
            wt.apply_diff(path, "this is not a diff\n")
    finally:
        wt.remove_worktree(repo, path, "rsi/test-2")


def test_provision_node_modules_skips_when_present(tmp_path):
    # Guard only: with node_modules already in place, provisioning is a
    # no-op and never shells out to npm (the live `npm ci` path is verified
    # by the pilot runs, not by unit tests).
    wt_path = tmp_path / "wt" / "web" / "portal"
    (wt_path / "node_modules").mkdir(parents=True)
    assert wt.provision_node_modules(tmp_path / "wt") is False
