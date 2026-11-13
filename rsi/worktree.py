"""git worktree management.

Borrowed from RRSI's gitops.py + loop.py idea: every candidate lives in its
own worktree on its own branch, off the baseline commit; the incumbent is
always a commit. Changed: morde-rsi never fast-forwards main. An adopted
candidate is committed to a branch named rsi/<task-id>; merging it is a
human-reviewed PR, never automatic.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def head_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def list_worktrees(repo: Path) -> list[dict]:
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    entries, cur = [], {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                entries.append(cur)
            cur = {"path": line[len("worktree "):]}
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):]
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line == "bare":
            cur["bare"] = True
        elif line == "detached":
            cur["detached"] = True
    if cur:
        entries.append(cur)
    return entries


def create_worktree(repo: Path, path: Path, branch: str,
                    start_point: str = "HEAD") -> Path:
    """git worktree add -b <branch> <path> <start_point>."""
    _git(repo, "worktree", "add", "-b", branch, str(path), start_point)
    return Path(path)


def diff_of_worktree(worktree: Path, base: str = "HEAD") -> str:
    """Uncommitted + untracked (tracked only) diff of a worktree vs base.

    Untracked new files are included via `git add -N` (intent-to-add) so the
    diff is complete without staging anything.
    """
    _git(worktree, "add", "-N", ".")
    r = _git(worktree, "diff", base, check=False)
    return r.stdout


def apply_diff(worktree: Path, diff: str) -> None:
    """Apply a unified diff inside a worktree (used for the clean Judge tree)."""
    r = subprocess.run(["git", "-C", str(worktree), "apply", "--whitespace=nowarn"],
                       input=diff, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git apply failed: {r.stderr.strip()}")


def commit_worktree(worktree: Path, message: str) -> str:
    """Commit everything in a worktree; returns the new commit sha."""
    _git(worktree, "add", "-A")
    r = _git(worktree, "commit", "-m", message, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git commit failed: {r.stdout.strip()} {r.stderr.strip()}")
    return head_commit(worktree)


def provision_node_modules(worktree: Path, timeout_s: int = 300) -> bool:
    """Install Portal dependencies into a worktree with `npm ci`.

    `git worktree add` only checks out tracked files; node_modules is
    gitignored, so a fresh worktree cannot run tsc/vitest without it.
    A symlink does NOT work: Vite's module runner denies files whose
    realpath is outside the project root (the pdfjs worker import in
    App.test.tsx / Assistant.test.tsx fails with "Denied ID ..."). `npm ci`
    (lockfile-exact) takes ~6s warm and puts a real directory inside the
    worktree. node_modules is gitignored, so `git add -A` ignores it;
    `tsc -b` writes its build info under node_modules/.tmp.
    Returns False (no-op) when the worktree already has node_modules.
    """
    dst = Path(worktree) / "web" / "portal"
    nm = dst / "node_modules"
    if not dst.is_dir() or nm.exists() or nm.is_symlink():
        return False
    r = subprocess.run(["npm", "ci", "--no-audit", "--no-fund"],
                       cwd=str(dst), capture_output=True, text=True,
                       timeout=timeout_s)
    if r.returncode != 0:
        raise RuntimeError(f"npm ci failed in {dst}: "
                           f"{r.stdout[-2000:]} {r.stderr[-2000:]}")
    return True


def remove_worktree(repo: Path, path: Path, branch: str | None = None) -> None:
    """Remove a worktree and, if given, delete its branch."""
    _git(repo, "worktree", "remove", "--force", str(path), check=False)
    if branch:
        _git(repo, "branch", "-D", branch, check=False)
