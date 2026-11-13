"""Tests for service/github_pr.py (PR body rendering + PR creation).

Run: cd <repo root> && .venv/bin/python -m pytest service/tests/test_github_pr.py -q
"""
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from service.github_pr import create_pr, render_pr_body  # noqa: E402


def _receipt(**over):
    r = {"job_id": "job-1", "tokens_in": 1200, "tokens_out": 340,
         "usd": None, "wall_seconds": 42.5, "verdict": "ADOPT",
         "branch": "rsi/CAND-001", "pr_url": None}
    r.update(over)
    return r


def _tiers():
    return {
        "A": {"passed": True, "status": "复现测试通过"},
        "B": {"passed": True, "status": "529/529"},
        "C": {"passed": True, "status": "3/3"},
    }


def test_render_pr_body_contains_verdict_scores_tiers_cost_branch():
    body = render_pr_body("ADOPT", 0.6, 0.8, _tiers(), _receipt())
    assert "ADOPT" in body
    assert "基线分数" in body and "0.6" in body
    assert "候选分数" in body and "0.8" in body
    for name in ("A", "B", "C"):
        assert name in body
    assert "通过" in body
    assert "1200" in body and "340" in body
    assert "42.5" in body
    assert "rsi/CAND-001" in body


def test_render_pr_body_usd_none_says_not_billed():
    body = render_pr_body("ADOPT", 0.6, 0.8, _tiers(), _receipt(usd=None))
    assert "未配置价格，未计费" in body
    assert "$" not in body.split("费用")[1].split("\n")[0]


def test_render_pr_body_usd_present_shows_amount():
    body = render_pr_body("ADOPT", 0.6, 0.8, _tiers(), _receipt(usd=0.0123))
    assert "0.0123" in body
    assert "未配置价格" not in body


def test_render_pr_body_never_auto_merge_and_evidence():
    body = render_pr_body("REJECT", 0.6, 0.8,
                          {"A": {"passed": False, "status": "复现失败"}},
                          _receipt())
    assert "永不自动合并：请人工审阅后手动合并" in body
    assert "失败" in body
    assert "runs/service/jobs/" in body  # default evidence note


def test_render_pr_body_custom_evidence_note_and_branch_arg():
    body = render_pr_body("ADOPT", 0.6, 0.8, _tiers(), _receipt(),
                          evidence_note="证据见附件日志。",
                          branch="rsi/CAND-002")
    assert "证据见附件日志。" in body
    assert "rsi/CAND-002" in body


def _init_git_repo(path: Path, origin: str | None = None):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                        origin], check=True)


def test_create_pr_no_origin_returns_none(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    assert create_pr(repo, "rsi/CAND-001", "main", "t", "b",
                     {"github": {"token_env": "GITHUB_TOKEN"}}) is None


def test_create_pr_unparseable_origin_returns_none(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, "git@gitlab.example.com:o/r.git")
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    assert create_pr(repo, "rsi/CAND-001", "main", "t", "b",
                     {"github": {"token_env": "GITHUB_TOKEN"}}) is None


def test_create_pr_no_token_no_gh_returns_none(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, "https://github.com/o/r.git")
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert create_pr(repo, "rsi/CAND-001", "main", "t", "b",
                     {"github": {"token_env": "GITHUB_TOKEN"}}) is None


class _StubHandler(BaseHTTPRequestHandler):
    status = 201
    payload = {"html_url": "https://github.com/o/r/pull/1"}
    seen = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).seen = json.loads(raw.decode("utf-8"))
        body = json.dumps(type(self).payload).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_stub(status=201, payload=None):
    _StubHandler.status = status
    _StubHandler.payload = payload or {"html_url": "https://github.com/o/r/pull/1"}
    _StubHandler.seen = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_create_pr_rest_success_returns_url(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, "https://github.com/o/r.git")
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    server = _start_stub(201, {"html_url": "https://github.com/o/r/pull/1"})
    try:
        url = create_pr(repo, "rsi/CAND-001", "main", "修复 routed 分支",
                        "正文", {"github": {"token_env": "GITHUB_TOKEN"}},
                        api_base=f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
    assert url == "https://github.com/o/r/pull/1"
    seen = _StubHandler.seen
    assert seen["title"] == "修复 routed 分支"
    assert seen["head"] == "rsi/CAND-001"
    assert seen["base"] == "main"
    assert seen["body"] == "正文"


def test_create_pr_rest_422_raises(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, "git@github.com:o/r.git")
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    server = _start_stub(422, {"message": "Validation Failed"})
    try:
        import pytest
        with pytest.raises(RuntimeError, match="422"):
            create_pr(repo, "rsi/CAND-001", "main", "t", "b",
                      {"github": {"token_env": "GITHUB_TOKEN"}},
                      api_base=f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
