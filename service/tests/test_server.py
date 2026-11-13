"""Tests for the morde-rsi HTTP server layer (service/server.py).

Spins up create_server on an ephemeral port with a tmp JobStore and an
inline config; exercises the routes over real HTTP via urllib.
"""
import json
import sys
import threading
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _ensure_service_package():
    """service.py (repo-root entry point) shadows the service/ package on
    import when service/__init__.py does not exist; register the
    directory as a namespace package so `import service.*` works."""
    import importlib.machinery
    import importlib.util

    existing = sys.modules.get("service")
    if existing is not None and getattr(existing, "__path__", None):
        return
    spec = importlib.util.find_spec("service")
    if spec is not None and spec.submodule_search_locations is not None:
        return
    pkg_spec = importlib.machinery.ModuleSpec(
        "service", loader=None, is_package=True)
    pkg_spec.submodule_search_locations = [str(REPO_ROOT / "service")]
    sys.modules["service"] = importlib.util.module_from_spec(pkg_spec)


_ensure_service_package()

import pytest  # noqa: E402

from service.server import create_server  # noqa: E402
from service.store import JobStore  # noqa: E402


def _config(tmp_path):
    return {
        "host": "127.0.0.1",
        "port": 0,
        "api_token": "test-token",
        "data_dir": str(tmp_path / "data"),
        "domains": {
            "morde": {"repo": "/tmp/morde"},
            "self": {"repo": "/tmp/self"},
        },
    }


@pytest.fixture()
def srv(tmp_path):
    cfg = _config(tmp_path)
    store = JobStore(tmp_path / "jobs.sqlite3")
    server = create_server(cfg, store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield {"base": base, "config": cfg, "store": store, "tmp": tmp_path,
           "server": server}
    server.shutdown()
    server.server_close()


def _req(base, method, path, body=None, token="test-token"):
    data = json.dumps(body).encode() if body is not None else None
    req = urlrequest.Request(base + path, data=data, method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlrequest.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except HTTPError as exc:
        raw = exc.read().decode()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
        return exc.code, payload


def _create_job(base, **overrides):
    body = {"domain": "morde", "task_id": "CAND-001"}
    body.update(overrides)
    return _req(base, "POST", "/v1/jobs", body)


def test_health_public(srv):
    status, payload = _req(srv["base"], "GET", "/health", token=None)
    assert status == 200
    assert payload == {"status": "ok", "service": "morde-rsi"}


def test_jobs_requires_auth(srv):
    status, payload = _req(srv["base"], "GET", "/v1/jobs", token=None)
    assert status == 401
    assert payload == {"error": "unauthorized"}
    status, payload = _req(srv["base"], "GET", "/v1/jobs", token="wrong")
    assert status == 401
    assert payload == {"error": "unauthorized"}


def test_create_and_get_job(srv):
    status, payload = _create_job(srv["base"])
    assert status == 202
    assert payload["status"] == "queued"
    job_id = payload["job_id"]
    assert job_id

    status, payload = _req(srv["base"], "GET", f"/v1/jobs/{job_id}")
    assert status == 200
    assert payload["id"] == job_id
    assert payload["status"] == "queued"

    status, payload = _req(srv["base"], "GET", "/v1/jobs")
    assert status == 200
    assert any(j["id"] == job_id for j in payload["jobs"])


def test_create_invalid_domain(srv):
    status, payload = _create_job(srv["base"], domain="nope")
    assert status == 400


def test_create_task_id_and_task_both_missing(srv):
    status, payload = _req(srv["base"], "POST", "/v1/jobs",
                           {"domain": "morde"})
    assert status == 400
    # Both present is also invalid: exactly one of task_id / task.
    status, payload = _req(srv["base"], "POST", "/v1/jobs", {
        "domain": "morde", "task_id": "CAND-001",
        "task": {"task_id": "CAND-001"}})
    assert status == 400


def test_create_task_dict(srv):
    status, payload = _req(srv["base"], "POST", "/v1/jobs", {
        "domain": "morde", "task": {"task_id": "CAND-002"}})
    assert status == 202
    assert payload["status"] == "queued"


def test_create_manual_fixer_without_diff(srv):
    status, payload = _create_job(
        srv["base"], task_id="CAND-002", fixer="manual")
    assert status == 400
    status, payload = _create_job(
        srv["base"], task_id="CAND-002", fixer="manual", diff="--- a/x\n")
    assert status == 202


def test_get_unknown_job(srv):
    status, payload = _req(srv["base"], "GET", "/v1/jobs/does-not-exist")
    assert status == 404
    assert payload == {"error": "job not found"}


def test_evidence_before_done(srv):
    _, created = _create_job(srv["base"])
    job_id = created["job_id"]
    status, payload = _req(srv["base"], "GET", f"/v1/jobs/{job_id}/evidence")
    assert status == 200
    assert payload["job_id"] == job_id
    assert payload["status"] == "queued"
    assert payload["reward"] is None
    assert payload["tier_logs"] == {}
    assert "after the job finishes" in payload["note"]


def test_feedback_validation_and_valid(srv):
    _, created = _create_job(srv["base"])
    job_id = created["job_id"]
    url = f"/v1/jobs/{job_id}/feedback"

    status, _ = _req(srv["base"], "POST", url,
                     {"kind": "bogus", "note": "x"})
    assert status == 400
    status, _ = _req(srv["base"], "POST", url,
                     {"kind": "human_label", "label": "bogus", "note": "x"})
    assert status == 400
    status, _ = _req(srv["base"], "POST", url, {"kind": "human_label"})
    assert status == 400
    status, _ = _req(srv["base"], "POST", url,
                     {"kind": "human_label", "note": "   "})
    assert status == 400

    status, payload = _req(srv["base"], "POST", url, {
        "kind": "human_label", "label": "correct",
        "note": "修对了，路由状态显示正常", "round": 0})
    assert status == 201
    assert payload["candidate_id"] == "CAND-001-c1"
    assert payload["task_id"] == "CAND-001"
    assert payload["kind"] == "human_label"
    assert payload["label"] == "correct"
    assert payload["source"] == "human"

    log_path = (srv["tmp"] / "data" / "jobs" / job_id / "runs"
                / "human_feedback.jsonl")
    records = [json.loads(line) for line in
               log_path.read_text().splitlines() if line.strip()]
    assert any(r["candidate_id"] == "CAND-001-c1"
               and r["label"] == "correct" for r in records)


def test_cancel_queued_then_again(srv):
    _, created = _create_job(srv["base"])
    job_id = created["job_id"]
    status, payload = _req(srv["base"], "POST", f"/v1/jobs/{job_id}/cancel")
    assert status == 200
    assert payload["status"] == "cancelled"
    status, payload = _req(srv["base"], "POST", f"/v1/jobs/{job_id}/cancel")
    assert status == 409


def test_frontier_no_done_jobs(srv):
    status, payload = _req(
        srv["base"], "GET", "/v1/frontier?domain=morde")
    assert status == 200
    assert payload == {"domain": "morde", "frontier": {}}
    status, _ = _req(srv["base"], "GET", "/v1/frontier")
    assert status == 400
    status, _ = _req(srv["base"], "GET", "/v1/frontier?domain=nope")
    assert status == 400


def test_unknown_route_and_wrong_method(srv):
    status, payload = _req(srv["base"], "GET", "/v1/nope")
    assert status == 404
    assert payload == {"error": "not found"}
    status, payload = _req(srv["base"], "DELETE", "/v1/jobs")
    assert status == 405


def test_malformed_json(srv):
    req = urlrequest.Request(
        srv["base"] + "/v1/jobs", data=b"{not json", method="POST")
    req.add_header("Authorization", "Bearer test-token")
    req.add_header("Content-Type", "application/json")
    try:
        with urlrequest.urlopen(req, timeout=5) as resp:
            status, payload = resp.status, json.loads(resp.read().decode())
    except HTTPError as exc:
        status, payload = exc.code, json.loads(exc.read().decode())
    assert status == 400
    assert "error" in payload
