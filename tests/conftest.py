"""Shared fixtures: a micro git repo with a known bug, never Morde's code.

The fixture project:
  calc.py       def add(a, b): return a + b + 1   # the bug
  ops.py        def sub(a, b): return a - b       # correct, guards P2P
  test_calc.py  test_add: add(1, 2) == 3          # F2P: fails at baseline
  test_other.py test_sub: sub(5, 2) == 3          # P2P: passes at baseline
  held-out:     test_heldout.py::test_add_negative: add(-1, 1) == 0
                (fails at baseline, passes after the fix; the Fixer never
                sees this file)
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

CALC_BUG = "def add(a, b):\n    return a + b + 1\n"
CALC_FIX = "def add(a, b):\n    return a + b\n"
OPS = "def sub(a, b):\n    return a - b\n"
TEST_CALC = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
TEST_OTHER = "from ops import sub\n\n\ndef test_sub():\n    assert sub(5, 2) == 3\n"
TEST_HELDOUT = "from calc import add\n\n\ndef test_add_negative():\n    assert add(-1, 1) == 0\n"

FIX_DIFF = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b + 1
+    return a + b
"""

BAD_DIFF = """diff --git a/ops.py b/ops.py
--- a/ops.py
+++ b/ops.py
@@ -1,2 +1,2 @@
 def sub(a, b):
-    return a - b
+    return a + b
"""

GAMING_DIFF = """diff --git a/test_calc.py b/test_calc.py
--- a/test_calc.py
+++ b/test_calc.py
@@ -1,5 +1,5 @@
 from calc import add
 
 
 def test_add():
-    assert add(1, 2) == 3
+    assert True
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "rsi-test@example.invalid")
    _git(repo, "config", "user.name", "rsi-test")
    (repo / "calc.py").write_text(CALC_BUG)
    (repo / "ops.py").write_text(OPS)
    (repo / "test_calc.py").write_text(TEST_CALC)
    (repo / "test_other.py").write_text(TEST_OTHER)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fixture: buggy add")
    return repo


@pytest.fixture()
def heldout_dir(tmp_path: Path) -> Path:
    d = tmp_path / "heldout"
    d.mkdir()
    (d / "test_heldout.py").write_text(TEST_HELDOUT)
    return d


def make_task(task_id: str = "FIX-001", heldout_src: Path | None = None) -> dict:
    """A task whose tiers run pytest in the fixture worktree."""
    py = sys.executable
    task = {
        "task_id": task_id,
        "title": "add() is off by one",
        "description": "add(1, 2) returns 4, expected 3.",
        "repro": "python -m pytest -q test_calc.py::test_add",
        "component": "logic-small",
        "hypothesis": "remove the spurious +1",
        "tiers": {
            "A": {"commands": [[py, "-m", "pytest", "-q",
                                "test_calc.py::test_add"]],
                  "timeout_s": 120},
            "B": {"commands": [[py, "-m", "pytest", "-q", "test_other.py"]],
                  "timeout_s": 120},
            "C": {"commands": [[py, "-m", "pytest", "-q", "test_heldout.py"]],
                  "timeout_s": 120,
                  "heldout": {"dest": "."}},
        },
    }
    if heldout_src is not None:
        task["tiers"]["C"]["heldout"]["src"] = str(heldout_src)
    return task


@pytest.fixture()
def task_json(tmp_path: Path, heldout_dir: Path) -> Path:
    p = tmp_path / "task.json"
    p.write_text(json.dumps(make_task(heldout_src=heldout_dir)))
    return p


def write_tasks_file(tmp_path: Path, tasks: list[dict]) -> Path:
    p = tmp_path / "evolve.json"
    p.write_text(json.dumps({"tasks": tasks}))
    return p
