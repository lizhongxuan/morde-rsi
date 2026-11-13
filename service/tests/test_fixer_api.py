"""Tests for service.fixer_api (APIFixer).

Uses an in-process stub model HTTP server; no real network, no real key.
"""
import json
import os
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# --- service.* siblings (implemented by parallel agents) -------------------
# If they are not present yet, install minimal contract-compatible stubs
# so these tests are self-contained. A real sibling module always wins.
def _config_stub():
    import os as _os
    from pathlib import Path as _Path

    mod = types.ModuleType("service.config")

    def load_config(path=None):
        cfg = {
            "host": "127.0.0.1", "port": 8799, "data_dir": "runs/service",
            "api_token": None,
            "fixer": {"provider": "deepseek",
                      "base_url": "https://api.deepseek.com/v1",
                      "model": "deepseek-chat", "api_key_env": "DEEPSEEK_API_KEY",
                      "timeout_s": 120, "max_tokens": 8192,
                      "price_per_1m_input_usd": None,
                      "price_per_1m_output_usd": None},
            "budgets": {"max_submissions": 3, "time_budget_s": 3600,
                        "max_usd_per_job": None},
            "github": {"create_pr": False, "token_env": "GITHUB_TOKEN"},
            "domains": {"morde": {"repo": "/home/hatch/workspace/morde-check"},
                        "self": {"repo": "/home/hatch/workspace/morde-rsi"}},
        }
        if path:
            p = _Path(path)
            if p.exists():
                cfg.update(json.loads(p.read_text()))
        return cfg

    def resolve_api_key(cfg):
        return _os.environ.get(
            (cfg.get("fixer") or {}).get("api_key_env", "DEEPSEEK_API_KEY"))

    def resolve_github_token(cfg):
        return _os.environ.get(
            (cfg.get("github") or {}).get("token_env", "GITHUB_TOKEN"))

    def resolve_data_dir(cfg):
        return _Path(cfg.get("data_dir") or "runs/service")

    mod.load_config = load_config
    mod.resolve_api_key = resolve_api_key
    mod.resolve_github_token = resolve_github_token
    mod.resolve_data_dir = resolve_data_dir
    return mod


def _store_stub():
    import json as _json
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    mod = types.ModuleType("service.store")

    class JobStore:
        def __init__(self, path):
            self.path = Path(path)
            self.path.mkdir(parents=True, exist_ok=True)
            self._file = self.path / "jobs.json"
            if not self._file.exists():
                self._file.write_text("[]")

        def _load(self):
            return _json.loads(self._file.read_text())

        def _save(self, jobs):
            self._file.write_text(
                _json.dumps(jobs, indent=1, ensure_ascii=False))

        def create_job(self, domain, repo=None, task_id=None, task=None,
                       fixer="api", diff=None):
            now = _dt.now(_tz.utc).isoformat()
            job = {"id": "job-" + _uuid.uuid4().hex[:12], "domain": domain,
                   "repo": repo, "task_id": task_id, "task": task,
                   "fixer": fixer, "diff": diff, "status": "queued",
                   "result": None, "cost": None, "created": now,
                   "updated": now}
            jobs = self._load()
            jobs.append(job)
            self._save(jobs)
            return dict(job)

        def get_job(self, job_id):
            for j in self._load():
                if j["id"] == job_id:
                    return dict(j)
            return None

        def list_jobs(self, domain=None, status=None, limit=50):
            out = [j for j in self._load()
                   if (domain is None or j["domain"] == domain)
                   and (status is None or j["status"] == status)]
            return [dict(j) for j in out[:limit]]

        def update_job(self, job_id, status=None, result=None, cost=None):
            jobs = self._load()
            for j in jobs:
                if j["id"] == job_id:
                    if status is not None:
                        j["status"] = status
                    if result is not None:
                        j["result"] = result
                    if cost is not None:
                        j["cost"] = cost
                    j["updated"] = _dt.now(_tz.utc).isoformat()
                    self._save(jobs)
                    return dict(j)
            return None

        def claim_next(self):
            jobs = self._load()
            for j in jobs:
                if j["status"] == "queued":
                    j["status"] = "running"
                    j["updated"] = _dt.now(_tz.utc).isoformat()
                    self._save(jobs)
                    return dict(j)
            return None

        def cancel_job(self, job_id):
            return self.update_job(job_id, status="cancelled")

    mod.JobStore = JobStore
    return mod


def _receipt_stub():
    mod = types.ModuleType("service.receipt")

    def compute_usd(tokens_in, tokens_out, price_in, price_out):
        if price_in is None or price_out is None:
            return None
        return (tokens_in / 1_000_000 * price_in
                + tokens_out / 1_000_000 * price_out)

    def build_receipt(job_id, tokens_in=0, tokens_out=0, usd=None,
                      wall_seconds=0.0, verdict=None, branch=None,
                      pr_url=None):
        return {"job_id": job_id, "tokens_in": tokens_in,
                "tokens_out": tokens_out, "usd": usd,
                "wall_seconds": wall_seconds, "verdict": verdict,
                "branch": branch, "pr_url": pr_url}

    def write_receipt(data_dir, job_id, receipt):
        p = Path(data_dir) / "receipts" / f"{job_id}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(receipt, indent=1, ensure_ascii=False))
        return p

    mod.compute_usd = compute_usd
    mod.build_receipt = build_receipt
    mod.write_receipt = write_receipt
    return mod


def _github_pr_stub():
    mod = types.ModuleType("service.github_pr")

    def create_pr(repo_path, branch, base, title, body, config,
                  api_base="https://api.github.com"):
        return None

    def render_pr_body(verdict, baseline_score, candidate_score, tiers,
                       receipt, evidence_note=None, branch=None):
        return (f"RSI {verdict}: {baseline_score} -> {candidate_score} "
                f"(branch {branch})")

    mod.create_pr = create_pr
    mod.render_pr_body = render_pr_body
    return mod


def _ensure_service_deps():
    import importlib
    for name, factory in (("config", _config_stub), ("store", _store_stub),
                          ("receipt", _receipt_stub),
                          ("github_pr", _github_pr_stub)):
        try:
            importlib.import_module(f"service.{name}")
        except ImportError:
            sys.modules[f"service.{name}"] = factory()


_ensure_service_deps()

from service.fixer_api import APIFixer, CostCapExceeded  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "test",
           "GIT_COMMITTER_EMAIL": "test@example.com"}

CALC_PY = "def add(a, b):\n    return a - b\n"
DIFF = ("diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n")
TASK = {"task_id": "T-1", "description": "Fix add so it returns the sum.",
        "repro": "pytest -q test_repro_add.py"}


def _ok_payload(diff, prompt=100, completion=50):
    return {"choices": [{"message": {"content": f"```diff\n{diff}```"}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


@pytest.fixture()
def stub_server():
    servers = []

    def _make(responses):
        state = {"requests": [], "n": 0}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    state["requests"].append(
                        json.loads(raw) if raw else None)
                except json.JSONDecodeError:
                    state["requests"].append(None)
                idx = min(state["n"], len(responses) - 1)
                state["n"] += 1
                status, payload = responses[idx]
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv, state

    yield _make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _make_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, env=env,
                   check=True)
    return repo


def _fixer(server, **kw):
    port = server.server_address[1]
    return APIFixer(base_url=f"http://127.0.0.1:{port}", model="stub",
                    api_key="test-key", **kw)


def test_happy_path_returns_exact_diff_and_tokens(tmp_path, stub_server):
    repo = _make_repo(tmp_path, {"calc.py": CALC_PY})
    srv, state = stub_server([(200, _ok_payload(DIFF))])
    fixer = _fixer(srv)
    result = fixer.fix(TASK, repo, {"max_submissions": 3})
    assert result.diff == DIFF
    assert result.cost_tokens == 150
    assert result.submissions == 1
    assert fixer.tokens_in == 100 and fixer.tokens_out == 50
    assert fixer.usd_spent is None  # no prices configured
    assert "api-fixer: model=stub tokens=150" == result.detail
    assert (repo / "calc.py").read_text() == (
        "def add(a, b):\n    return a + b\n")
    # The request carried the brief and no key material in the URL.
    assert state["requests"][0]["model"] == "stub"
    assert "Fix add" in state["requests"][0]["messages"][1]["content"]


def test_plain_fence_diff_accepted(tmp_path, stub_server):
    repo = _make_repo(tmp_path, {"calc.py": CALC_PY})
    payload = {"choices": [{"message": {"content": f"```\n{DIFF}```"}}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    srv, _ = stub_server([(200, payload)])
    result = _fixer(srv).fix(TASK, repo, {"max_submissions": 3})
    assert result.diff == DIFF
    assert result.cost_tokens == 15


def test_bad_diff_fails_git_apply_check(tmp_path, stub_server):
    repo = _make_repo(tmp_path, {"calc.py": CALC_PY})
    srv, _ = stub_server([(200, _ok_payload("not a diff at all\n"))])
    with pytest.raises(RuntimeError, match="git apply --check failed"):
        _fixer(srv).fix(TASK, repo, {"max_submissions": 3})


def test_no_diff_block_raises(tmp_path, stub_server):
    repo = _make_repo(tmp_path, {"calc.py": CALC_PY})
    payload = {"choices": [{"message": {"content": "I fixed it, trust me."}}],
               "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    srv, _ = stub_server([(200, payload)])
    with pytest.raises(RuntimeError, match="no diff block in model response"):
        _fixer(srv).fix(TASK, repo, {"max_submissions": 3})


def test_429_then_success_retries(tmp_path, stub_server):
    repo = _make_repo(tmp_path, {"calc.py": CALC_PY})
    srv, state = stub_server([(429, {"error": {"message": "slow down"}}),
                              (200, _ok_payload(DIFF))])
    result = _fixer(srv).fix(TASK, repo, {"max_submissions": 3})
    assert result.diff == DIFF
    assert len(state["requests"]) == 2


def test_400_raises_without_retry(tmp_path, stub_server):
    repo = _make_repo(tmp_path, {"calc.py": CALC_PY})
    srv, state = stub_server([(400, {"error": {"message": "bad request"}})])
    with pytest.raises(RuntimeError, match="HTTP 400"):
        _fixer(srv).fix(TASK, repo, {"max_submissions": 3})
    assert len(state["requests"]) == 1


def test_cost_cap_exceeded(tmp_path, stub_server):
    repo = _make_repo(tmp_path, {"calc.py": CALC_PY})
    srv, _ = stub_server([(200, _ok_payload(DIFF))])
    # 100 in @ $1.00/1m + 50 out @ $2.00/1m = $0.0002 > $0.0001 cap.
    fixer = _fixer(srv, price_per_1m_input_usd=1.0,
                   price_per_1m_output_usd=2.0, max_usd_per_job=0.0001)
    with pytest.raises(CostCapExceeded) as ei:
        fixer.fix(TASK, repo, {"max_submissions": 3})
    assert ei.value.spent_usd == pytest.approx(0.0002)
    assert ei.value.cap_usd == 0.0001


def test_brief_leak_check_rejects_before_http_call(tmp_path, stub_server):
    repo = _make_repo(tmp_path, {"calc.py": CALC_PY})
    srv, state = stub_server([(200, _ok_payload(DIFF))])
    task = dict(TASK)
    task["description"] = "Fix the helper used by test_heldout_add.py."
    task["heldout_markers"] = ["test_heldout_add.py"]
    with pytest.raises(RuntimeError, match="brief leaked verifier material"):
        _fixer(srv).fix(task, repo, {"max_submissions": 3})
    assert state["requests"] == []  # no model call was made
