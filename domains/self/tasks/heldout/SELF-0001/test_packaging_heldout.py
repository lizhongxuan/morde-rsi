"""Tier C held-out for SELF-0001 (verifier-injected, the Fixer never sees it).

A plain editable install WITHOUT the [dev] extra must succeed AND the
installed package must import: `import rsi` plus the key entry points.
This checks the fix generalises beyond the [dev] extra instead of
special-casing it: tier A only proves `.[dev]` installs, this proves the
package itself is installable and importable.

The import check runs with cwd OUTSIDE the repo (tmp_path), so the
imports resolve through the installed (editable) package, not through
the working directory.

Verified 2026-11-13: at baseline the plain install fails with the same
setuptools flat-layout error as the [dev] install, so this test fails
before the fix and must pass after it.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # tests/ -> repo root

ENTRY_POINTS = ["rsi", "rsi.controller", "rsi.verifier", "rsi.critic",
                "rsi.selection", "rsi.feedback", "rsi.analyzer",
                "rsi.history", "rsi.fixer", "rsi.config", "rsi.schedule",
                "rsi.worktree"]


def test_plain_editable_install_imports(tmp_path):
    venv = tmp_path / "venv"
    r = subprocess.run([sys.executable, "-m", "venv", str(venv)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    pip = venv / "bin" / "pip"
    r = subprocess.run([str(pip), "install", "-e", "."],
                       cwd=str(ROOT), capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, (r.stdout[-2000:] + r.stderr[-2000:])
    check = "; ".join(f"import {m}" for m in ENTRY_POINTS)
    r = subprocess.run([str(venv / "bin" / "python"), "-c", check],
                       cwd=str(tmp_path), capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
