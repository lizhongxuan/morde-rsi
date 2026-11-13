"""GitHub PR creation for the morde-rsi HTTP service.

Two jobs, both pure plumbing around a candidate the loop already verified:

* :func:`render_pr_body` -- render the PR description (pure function, no
  network). It carries the verdict, the baseline/candidate scores, the
  per-tier results, the cost receipt and an evidence pointer.
* :func:`create_pr` -- open the PR, preferring the ``gh`` CLI when it is
  installed and falling back to the GitHub REST API over urllib.

This module NEVER merges anything. There is no merge code here, on
purpose: adopting a candidate only ever produces a ``rsi/<task-id>``
branch; merging into main is a human reviewing the PR.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

NEVER_AUTO_MERGE = "永不自动合并：请人工审阅后手动合并"

# git@github.com:OWNER/REPO(.git)?  or  https://github.com/OWNER/REPO(.git)?
_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|https://github\.com/)"
    r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)


def parse_owner_repo(remote_url: str) -> tuple[str, str] | None:
    """Parse ``OWNER/REPO`` out of a GitHub remote URL, else None."""
    m = _REMOTE_RE.match(remote_url.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def render_pr_body(verdict, baseline_score, candidate_score, tiers, receipt,
                   evidence_note=None, branch=None) -> str:
    """Render the PR body for one candidate.

    ``tiers`` is ``{name: {"passed": bool, "status": str}}``.
    ``receipt`` is the cost receipt dict (see ``service/receipt.py``):
    ``tokens_in`` / ``tokens_out`` / ``usd`` (None when no price is
    configured) / ``wall_seconds`` / ``job_id``. ``usd`` is never invented:
    when it is None the body says so instead of showing a number.
    """
    receipt = receipt or {}
    branch = branch or receipt.get("branch") or "(未指定分支)"
    usd = receipt.get("usd")
    if usd is None:
        cost_line = "费用：未配置价格，未计费"
    else:
        cost_line = f"费用：${usd:.4f}"

    tier_lines = []
    for name, info in (tiers or {}).items():
        info = info or {}
        passed = info.get("passed")
        status = info.get("status") or ""
        mark = "✅ 通过" if passed else "❌ 失败"
        line = f"- {name}：{mark}"
        if status:
            line += f"（{status}）"
        tier_lines.append(line)
    tiers_text = "\n".join(tier_lines) if tier_lines else "- （无分层结果）"

    if evidence_note:
        evidence = evidence_note
    else:
        evidence = (
            "证据见 `runs/service/jobs/<job_id>/runs/` 下的验证日志与 "
            "`reward.json`（按本次任务的 job_id 查找）。"
        )

    return f"""# RSI 候选：{verdict}

> **{NEVER_AUTO_MERGE}**

## 分数

- 基线分数：{baseline_score}
- 候选分数：{candidate_score}

## 分层结果

{tiers_text}

## 成本

- 输入 tokens：{receipt.get("tokens_in", "—")}
- 输出 tokens：{receipt.get("tokens_out", "—")}
- {cost_line}
- 耗时：{receipt.get("wall_seconds", "—")} 秒

## 分支

`{branch}`

## 证据

{evidence}
"""


def _gh_create(owner: str, repo: str, branch: str, base: str,
               title: str, body: str) -> str:
    """Create the PR with the ``gh`` CLI; return the PR URL."""
    env = dict(os.environ)
    cmd = ["gh", "pr", "create",
           "--repo", f"{owner}/{repo}",
           "--base", base, "--head", branch,
           "--title", title, "--body", body]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"gh pr create 失败：{r.stderr.strip()}")
    return r.stdout.strip()


def _rest_create(api_base: str, owner: str, repo: str, branch: str, base: str,
                 title: str, body: str, token: str) -> str:
    """Create the PR with the GitHub REST API; return the PR URL."""
    url = f"{api_base.rstrip('/')}/repos/{owner}/{repo}/pulls"
    payload = json.dumps(
        {"title": title, "head": branch, "base": base, "body": body}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"GitHub API 创建 PR 失败：HTTP {e.code}：{detail}") from e
    return data["html_url"]


def create_pr(repo_path, branch, base, title, body, config,
              api_base="https://api.github.com") -> str | None:
    """Open a PR for ``branch`` against ``base``; return its URL or None.

    Returns None (does not raise) when a PR cannot be attempted at all:
    the repo has no ``origin`` remote, the remote URL is not a GitHub URL,
    or (REST path) no GitHub token is configured. Raises RuntimeError when
    the attempt is made but GitHub (or ``gh``) rejects it.

    Resolution order: ``gh`` CLI if installed, else the REST API with the
    token from ``config["github"]["token_env"]`` (default ``GITHUB_TOKEN``).
    Never merges -- this function only creates the PR.
    """
    r = subprocess.run(["git", "-C", str(repo_path), "remote", "get-url",
                        "origin"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    parsed = parse_owner_repo(r.stdout.strip())
    if parsed is None:
        return None
    owner, repo = parsed

    if shutil.which("gh"):
        return _gh_create(owner, repo, branch, base, title, body)

    token_env = (config or {}).get("github", {}).get("token_env") or "GITHUB_TOKEN"
    token = os.environ.get(token_env)
    if not token:
        return None
    return _rest_create(api_base, owner, repo, branch, base, title, body, token)
