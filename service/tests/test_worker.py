"""Tests for service.worker (Worker) end to end through the real RSI
Controller: a stub model server drives the API fixer to ADOPT on a
fixture git repo created in tmp (never the real morde-check repo).
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

from service.store import JobStore  # noqa: E402
from service.worker import Worker  # noqa: E402

VENV_PY = "/home/hatch/workspace/morde-rsi/.venv/bin/python"
GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "test",
           "GIT_COMMITTER_EMAIL": "test@example.com"}

CALC_PY = ("def add(a, b):\n    return a - b\n\n"
           "def sub(a, b):\n    return a - b\n")
TEST_CALC_PY = ("from calc import sub\n\n\n"
                "def test_sub():\n    assert sub(5, 3) == 2\n")
E2E_DIFF = ("diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
            "@@ -1,4 +1,4 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
            " \n def sub(a, b):\n     return a - b\n")


def _git(repo, *args, check=True):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", "-C", str(repo), *args], env=env,
                          capture_output=True, text=True, check=check)


@pytest.fixture()
def fixture_repo(tmp_path, monkeypatch):
    # Git identity in the process env: Controller.commit_worktree runs
    # `git commit` in a worktree, inheriting os.environ (not the env of
    # the fixture-creation calls below).
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "calc-repo"
    repo.mkdir()
    (repo / "calc.py").write_text(CALC_PY)
    (repo / "test_calc.py").write_text(TEST_CALC_PY)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    # Verifier-owned test dirs live outside the repo, as in production.
    repro = tmp_path / "repro"
    repro.mkdir()
    (repro / "test_repro_add.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    heldout = tmp_path / "heldout"
    heldout.mkdir()
    (heldout / "test_heldout_add.py").write_text(
        "from calc import add\n\n\ndef test_add_zero():\n"
        "    assert add(0, 0) == 0\n\n\ndef test_add_neg():\n"
        "    assert add(-1, 1) == 0\n")
    return repo, repro, heldout


def _e2e_task(repro, heldout):
    return {
        "task_id": "E2E-001",
        "component": "logic-small",
        "description": "add() returns a - b instead of a + b.",
        "hypothesis": "add() has the wrong operator; flip - to +.",
        "title": "add() subtracts instead of adding",
        "tiers": {
            "A": {"commands": [[VENV_PY, "-m", "pytest", "-q",
                                "test_repro_add.py"]],
                  "repro": {"src": str(repro), "dest": "."},
                  "timeout_s": 120},
            "B": {"commands": [[VENV_PY, "-m", "pytest", "-q",
                                "test_calc.py"]],
                  "timeout_s": 120},
            "C": {"commands": [[VENV_PY, "-m", "pytest", "-q",
                                "test_heldout_add.py"]],
                  "heldout": {"src": str(heldout), "dest": "."},
                  "timeout_s": 120},
        },
    }


@pytest.fixture()
def stub_model():
    """Stub chat-completions server always returning E2E_DIFF."""
    servers = []

    def _make():
        payload = {"choices": [{"message": {"content":
                                            f"```diff\n{E2E_DIFF}```"}}],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 50}}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _config(tmp_path, stub_url):
    return {
        "host": "127.0.0.1", "port": 8799,
        "data_dir": str(tmp_path / "data"), "api_token": None,
        "fixer": {"provider": "deepseek", "base_url": stub_url,
                  "model": "stub", "api_key_env": "DEEPSEEK_API_KEY",
                  "timeout_s": 30, "max_tokens": 8192,
                  "price_per_1m_input_usd": 1.0,
                  "price_per_1m_output_usd": 2.0},
        "budgets": {"max_submissions": 3, "time_budget_s": 3600,
                    "max_usd_per_job": 10.0},
        "github": {"create_pr": False, "token_env": "GITHUB_TOKEN"},
        "domains": {"morde": {"repo": "/home/hatch/workspace/morde-check"},
                    "self": {"repo": "/home/hatch/workspace/morde-rsi"}},
    }


def test_e2e_api_fixer_adopt(tmp_path, monkeypatch, fixture_repo,
                             stub_model):
    repo, repro, heldout = fixture_repo
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    srv = stub_model()
    port = srv.server_address[1]
    config = _config(tmp_path, f"http://127.0.0.1:{port}")
    store = JobStore(tmp_path / "store")
    # Domain "morde": the fixture repo is shaped like the morde domain.
    # (The self domain's SKILL.md previously tripped the brief leak-check
    # by naming ledger files; that is fixed — see
    # test_api_fixer_self_skill_md_passes_brief_check below.)
    store.create_job(domain="morde", repo=str(repo),
                     task=_e2e_task(repro, heldout), fixer="api")
    worker = Worker(config, store)
    result = worker.run_once()
    assert result is not None
    assert result["verdict"] == "ADOPT", result
    assert result["branch"] == "rsi/E2E-001"
    assert result["baseline_score"] == pytest.approx(1 / 3, abs=1e-3)
    assert result["score"] == pytest.approx(1.0)
    job = store.list_jobs()[0]
    assert job["status"] == "done"
    assert job["cost"]["tokens_in"] == 100
    assert job["cost"]["tokens_out"] == 50
    receipt_path = (tmp_path / "data" / "receipts"
                    / f"{job['id']}.json")
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text())
    assert receipt["verdict"] == "ADOPT"
    assert receipt["tokens_in"] == 100
    branches = _git(repo, "branch", "--list", "rsi/E2E-001").stdout
    assert "rsi/E2E-001" in branches
    # Queue is drained.
    assert worker.run_once() is None


def test_api_fixer_self_skill_md_passes_brief_check(fixture_repo, stub_model):
    """The self domain can use an LLM fixer (fixed 2026-11-13):
    domains/self/SKILL.md no longer names the feedback ledger files
    literally, so rsi/critic.py::review_brief accepts the rendered
    brief and APIFixer.fix() reaches the model and applies its diff.
    """
    repo, _, _ = fixture_repo
    srv = stub_model()
    port = srv.server_address[1]
    from rsi import critic as critic_mod
    from rsi.fixer import render_brief
    from service.fixer_api import APIFixer
    skill_md = (ROOT / "domains" / "self" / "SKILL.md").read_text()
    assert "telemetry.jsonl" not in skill_md
    brief = render_brief({"task_id": "E2E-001", "description": "x"},
                         repo, skill_md, 3)
    assert critic_mod.review_brief(brief)["verdict"] == "accept"
    fixer = APIFixer(base_url=f"http://127.0.0.1:{port}", model="stub",
                     api_key="test-key", skill_md=skill_md)
    result = fixer.fix({"task_id": "E2E-001", "description": "x",
                        "tiers": {}}, repo, {"max_submissions": 3})
    assert result.diff == E2E_DIFF
    assert "return a + b" in (repo / "calc.py").read_text()


def test_load_domain_not_poisoned_by_top_level_adapter_import():
    """Regression: a top-level `import adapter` of the self domain (as
    tests/test_critic.py does) must not make later morde jobs load the
    self adapter. The worker loads adapters by file location under a
    per-domain module name, so the sys.modules["adapter"] cache cannot
    cross domains. (Before the fix, the API E2E passed alone but returned
    STUCK in the full suite, because the self SKILL.md tripped the brief
    leak-check.)
    """
    import sys
    sys.path.insert(0, str(ROOT / "domains" / "self"))
    import adapter as self_adapter
    assert self_adapter.DOMAIN.name == "self"
    from service.worker import _load_domain
    assert _load_domain("morde").name == "morde"
    assert _load_domain("self").name == "self"


def test_e2e_manual_fixer_adopt(tmp_path, fixture_repo):
    repo, repro, heldout = fixture_repo
    config = _config(tmp_path, "http://127.0.0.1:1")
    store = JobStore(tmp_path / "store")
    store.create_job(domain="self", repo=str(repo),
                     task=_e2e_task(repro, heldout), fixer="manual",
                     diff=E2E_DIFF)
    worker = Worker(config, store)
    result = worker.run_once()
    assert result is not None
    assert result["verdict"] == "ADOPT", result
    assert result["branch"] == "rsi/E2E-001"
    assert store.list_jobs()[0]["status"] == "done"


def test_unknown_domain_fails_job(tmp_path):
    config = _config(tmp_path, "http://127.0.0.1:1")
    store = JobStore(tmp_path / "store")
    job = store.create_job(domain="nope", repo="/nonexistent",
                           task={"task_id": "X-1"})
    worker = Worker(config, store)
    result = worker.execute_job(job)
    assert "unknown domain" in result["error"]
    assert store.get_job(job["id"])["status"] == "failed"


def test_missing_api_key_fails_job(tmp_path, monkeypatch, fixture_repo):
    repo, repro, heldout = fixture_repo
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config = _config(tmp_path, "http://127.0.0.1:1")
    store = JobStore(tmp_path / "store")
    job = store.create_job(domain="self", repo=str(repo),
                           task=_e2e_task(repro, heldout), fixer="api")
    worker = Worker(config, store)
    result = worker.execute_job(job)
    assert "no model API key" in result["error"]
    assert store.get_job(job["id"])["status"] == "failed"
