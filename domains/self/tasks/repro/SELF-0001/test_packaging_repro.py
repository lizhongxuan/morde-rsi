"""Tier A repro for SELF-0001 (verifier-injected, the Fixer never sees it).

A fresh venv + `pip install -e ".[dev]"` must succeed. At baseline
setuptools refuses the flat layout and the install fails:

    error: Multiple top-level packages discovered in a flat-layout:
    ['rsi', 'refs', 'runs', 'domains'].

Verified 2026-11-13 in /tmp/self-repro-venv (fresh venv, repo HEAD
dc5acfd): the install fails with the error above; the build subprocess
exits 1. After the fix this test must pass.

The test builds the venv with sys.executable (the python running pytest,
i.e. the repo's own .venv python) and installs from the repo root the
test file was injected into. Building the venv and fetching the build
backend + dev extra from PyPI needs network.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # tests/ -> repo root


def test_fresh_venv_pip_install_dev(tmp_path):
    venv = tmp_path / "venv"
    r = subprocess.run([sys.executable, "-m", "venv", str(venv)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    pip = venv / "bin" / "pip"
    r = subprocess.run([str(pip), "install", "-e", ".[dev]"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, (r.stdout[-2000:] + r.stderr[-2000:])
