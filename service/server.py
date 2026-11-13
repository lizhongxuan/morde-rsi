"""HTTP server layer for the morde-rsi service.

Stdlib only (http.server, json, urllib). JSON only -- never HTML. Jobs are
async: handlers only enqueue / read state via the JobStore; execution lives
in service.worker and is never run inside a request handler.

Routes (all /v1/* routes require ``Authorization: Bearer <api_token>``;
GET /health is public):

    GET  /health                    {"status": "ok", "service": "morde-rsi"}
    POST /v1/jobs                  create a job -> 202 {"job_id", "status"}
    GET  /v1/jobs?domain=&status=&limit=
    GET  /v1/jobs/{id}
    GET  /v1/jobs/{id}/evidence
    POST /v1/jobs/{id}/feedback    human feedback -> 201 stored record
    POST /v1/jobs/{id}/cancel
    GET  /v1/frontier?domain=
"""
from __future__ import annotations

import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rsi.feedback import (
    FEEDBACK_KINDS,
    FEEDBACK_LABELS,
    FeedbackLog,
    FeedbackRecord,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Evidence / feedback for a job live under <data_dir>/jobs/<job_id>/runs/.
NOT_DONE_NOTE = "evidence available after the job finishes"


def _data_dir(config: dict) -> Path:
    """data_dir from config; relative paths resolve against the repo root."""
    p = Path(config.get("data_dir") or "runs/service")
    return p if p.is_absolute() else REPO_ROOT / p


def _job_runs_dir(config: dict, job_id: str) -> Path:
    return _data_dir(config) / "jobs" / job_id / "runs"


def _job_task_id(job: dict) -> str | None:
    task_id = job.get("task_id")
    if isinstance(task_id, str) and task_id:
        return task_id
    task = job.get("task")
    if isinstance(task, dict):
        task_id = task.get("task_id")
        if isinstance(task_id, str) and task_id:
            return task_id
    return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    @property
    def config(self) -> dict:
        return self.server.rsi_config  # type: ignore[attr-defined]

    @property
    def store(self):
        return self.server.rsi_store  # type: ignore[attr-defined]

    def log_message(self, *args):  # keep test / server output clean
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorized(self) -> bool:
        token = self.config.get("api_token")
        if not token:
            return False
        auth = self.headers.get("Authorization") or ""
        return hmac.compare_digest(auth, f"Bearer {token}")

    def _read_json_body(self):
        """Return (data, error). error is None on success."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "invalid Content-Length"
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return None, "empty body"
        try:
            return json.loads(raw.decode("utf-8")), None
        except (ValueError, UnicodeDecodeError) as exc:
            return None, f"malformed JSON: {exc}"

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _dispatch(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if len(path) > 1:
            path = path.rstrip("/")
        query = parse_qs(parsed.query)

        m_job = re.fullmatch(r"/v1/jobs/([^/]+)", path)
        m_sub = re.fullmatch(r"/v1/jobs/([^/]+)/([^/]+)", path)

        if path == "/health":
            if self.command != "GET":
                return self._json(405, {"error": "method not allowed"})
            return self._json(200, {"status": "ok", "service": "morde-rsi"})

        if path == "/v1/jobs":
            if self.command not in ("GET", "POST"):
                return self._json(405, {"error": "method not allowed"})
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            if self.command == "GET":
                return self._list_jobs(query)
            return self._create_job()

        if path == "/v1/frontier":
            if self.command != "GET":
                return self._json(405, {"error": "method not allowed"})
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            return self._frontier(query)

        if m_job:
            if self.command != "GET":
                return self._json(405, {"error": "method not allowed"})
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            return self._get_job(m_job.group(1))

        if m_sub:
            job_id, sub = m_sub.group(1), m_sub.group(2)
            if sub not in ("evidence", "feedback", "cancel"):
                return self._json(404, {"error": "not found"})
            method = {"evidence": "GET", "feedback": "POST", "cancel": "POST"}[sub]
            if self.command != method:
                return self._json(405, {"error": "method not allowed"})
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            if sub == "evidence":
                return self._evidence(job_id)
            if sub == "feedback":
                return self._feedback(job_id)
            return self._cancel(job_id)

        return self._json(404, {"error": "not found"})

    # -- handlers ---------------------------------------------------------
    def _create_job(self):
        body, err = self._read_json_body()
        if err is not None:
            return self._json(400, {"error": err})
        if not isinstance(body, dict):
            return self._json(400, {"error": "body must be a JSON object"})

        domain = body.get("domain")
        domains = self.config.get("domains") or {}
        if not isinstance(domain, str) or domain not in domains:
            return self._json(400, {"error": "unknown or missing domain"})

        has_task_id = body.get("task_id") is not None
        has_task = body.get("task") is not None
        if has_task_id == has_task:
            return self._json(
                400, {"error": "provide exactly one of task_id or task"})
        task_id = body.get("task_id")
        task = body.get("task")
        if has_task_id and not isinstance(task_id, str):
            return self._json(400, {"error": "task_id must be a string"})
        if has_task:
            if not isinstance(task, dict) or not isinstance(
                    task.get("task_id"), str):
                return self._json(
                    400, {"error": "task must be an object with a task_id string"})

        fixer = body.get("fixer", "api")
        if fixer not in ("api", "manual"):
            return self._json(400, {"error": "fixer must be 'api' or 'manual'"})
        diff = body.get("diff")
        if fixer == "manual" and not isinstance(diff, str):
            return self._json(
                400, {"error": "manual fixer requires a diff string"})

        job = self.store.create_job(
            domain,
            repo=body.get("repo"),
            task_id=task_id if has_task_id else None,
            task=task if has_task else None,
            fixer=fixer,
            diff=diff,
        )
        return self._json(
            202, {"job_id": job["id"], "status": job.get("status", "queued")})

    def _list_jobs(self, query):
        domain = (query.get("domain") or [None])[0]
        status = (query.get("status") or [None])[0]
        limit_raw = (query.get("limit") or [None])[0]
        limit = 50
        if limit_raw is not None:
            try:
                limit = int(limit_raw)
            except ValueError:
                return self._json(400, {"error": "limit must be an integer"})
        jobs = self.store.list_jobs(domain=domain, status=status, limit=limit)
        return self._json(200, {"jobs": jobs})

    def _get_job(self, job_id):
        job = self.store.get_job(job_id)
        if job is None:
            return self._json(404, {"error": "job not found"})
        return self._json(200, job)

    def _evidence(self, job_id):
        job = self.store.get_job(job_id)
        if job is None:
            return self._json(404, {"error": "job not found"})
        status = job.get("status")
        task_id = _job_task_id(job)
        if status != "done":
            return self._json(200, {
                "job_id": job_id, "status": status, "verdict": None,
                "reward": None, "tier_logs": {}, "note": NOT_DONE_NOTE,
            })
        runs_dir = _job_runs_dir(self.config, job_id)
        reward = None
        if task_id:
            reward_path = runs_dir / f"reward-{task_id}-c1.json"
            if reward_path.exists():
                try:
                    reward = json.loads(reward_path.read_text())
                except (ValueError, OSError):
                    reward = None
        tier_logs: dict[str, str] = {}
        if task_id:
            ev_dir = runs_dir / f"evidence-{task_id}"
            if ev_dir.is_dir():
                for log_path in sorted(ev_dir.glob("*.log"))[:5]:
                    try:
                        tier_logs[log_path.name] = log_path.read_text(
                            errors="replace")[-2000:]
                    except OSError:
                        continue
        verdict = None
        result = job.get("result")
        if isinstance(result, dict):
            verdict = result.get("verdict")
        return self._json(200, {
            "job_id": job_id, "status": status, "verdict": verdict,
            "reward": reward, "tier_logs": tier_logs, "note": "",
        })

    def _feedback(self, job_id):
        job = self.store.get_job(job_id)
        if job is None:
            return self._json(404, {"error": "job not found"})
        body, err = self._read_json_body()
        if err is not None:
            return self._json(400, {"error": err})
        if not isinstance(body, dict):
            return self._json(400, {"error": "body must be a JSON object"})

        kind = body.get("kind")
        if kind not in FEEDBACK_KINDS:
            return self._json(400, {"error": f"unknown feedback kind: {kind!r}"})
        label = body.get("label")
        if label is not None and label not in FEEDBACK_LABELS:
            return self._json(400, {"error": f"unknown feedback label: {label!r}"})
        note = body.get("note")
        if not isinstance(note, str) or not note.strip():
            return self._json(400, {"error": "note is required"})
        round_no = body.get("round")
        if round_no is not None and (
                isinstance(round_no, bool) or not isinstance(round_no, int)):
            return self._json(400, {"error": "round must be an integer"})

        task_id = _job_task_id(job)
        if not task_id:
            return self._json(400, {"error": "job has no task_id"})
        log_path = _job_runs_dir(self.config, job_id) / "human_feedback.jsonl"
        try:
            stored = FeedbackLog(log_path).append(FeedbackRecord(
                candidate_id=f"{task_id}-c1",
                task_id=task_id,
                kind=kind,
                note=note,
                source="human",
                label=label,
                round_no=round_no,
            ))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(201, stored)

    def _cancel(self, job_id):
        job = self.store.get_job(job_id)
        if job is None:
            return self._json(404, {"error": "job not found"})
        status = job.get("status")
        if status == "queued":
            updated = self.store.cancel_job(job_id)
            return self._json(200, updated if updated is not None else job)
        if status == "running":
            return self._json(
                409, {"error": "cannot cancel a running job in v1"})
        return self._json(409, {"error": f"job already {status}"})

    def _frontier(self, query):
        domain = (query.get("domain") or [None])[0]
        domains = self.config.get("domains") or {}
        if not isinstance(domain, str) or domain not in domains:
            return self._json(
                400, {"error": "domain is required and must be known"})
        frontier: dict = {}
        for job in self.store.list_jobs(
                domain=domain, status="done", limit=50):
            frontier_path = (
                _job_runs_dir(self.config, job["id"]) / "frontier.json")
            if frontier_path.exists():
                try:
                    frontier = json.loads(frontier_path.read_text())
                except (ValueError, OSError):
                    frontier = {}
                break
        return self._json(200, {"domain": domain, "frontier": frontier})


def create_server(config: dict, store) -> ThreadingHTTPServer:
    """Build (but do not start) the HTTP server for `config` + `store`."""
    server = ThreadingHTTPServer(
        (config.get("host", "127.0.0.1"), config.get("port", 8765)), _Handler)
    server.daemon_threads = True
    server.rsi_config = config  # type: ignore[attr-defined]
    server.rsi_store = store  # type: ignore[attr-defined]
    return server


def run_server(config: dict, store) -> None:
    """Block serving forever (until KeyboardInterrupt / shutdown)."""
    create_server(config, store).serve_forever()
