"""SQLite-backed job store for the morde-rsi HTTP service (stdlib only).

One row per submitted job. ``task``/``result``/``cost`` are stored as JSON
text and deserialized back to dicts on read. ``claim_next`` atomically
moves the oldest queued job to ``running`` with ``BEGIN IMMEDIATE`` so two
workers can never claim the same job.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

_SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    domain TEXT,
    repo TEXT,
    task_id TEXT,
    task_json TEXT,
    fixer TEXT,
    diff TEXT,
    status TEXT,
    result_json TEXT,
    cost_json TEXT,
    created REAL,
    updated REAL
)"""


def _row_to_dict(row: sqlite3.Row) -> dict:
    def _load(raw):
        return json.loads(raw) if raw is not None else None

    return {
        "id": row["id"],
        "domain": row["domain"],
        "repo": row["repo"],
        "task_id": row["task_id"],
        "task": _load(row["task_json"]),
        "fixer": row["fixer"],
        "diff": row["diff"],
        "status": row["status"],
        "result": _load(row["result_json"]),
        "cost": _load(row["cost_json"]),
        "created": row["created"],
        "updated": row["updated"],
    }


class JobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode: ordinary statements commit immediately, and
        # claim_next can manage its own BEGIN IMMEDIATE transaction.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None
        self._conn.execute(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def create_job(self, domain, repo=None, task_id=None, task=None,
                   fixer="api", diff=None) -> dict:
        job_id = uuid.uuid4().hex
        now = time.time()
        self._conn.execute(
            "INSERT INTO jobs (id, domain, repo, task_id, task_json, fixer,"
            " diff, status, result_json, cost_json, created, updated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                domain,
                repo,
                task_id,
                json.dumps(task) if task is not None else None,
                fixer,
                diff,
                "queued",
                None,
                None,
                now,
                now,
            ),
        )
        return self.get_job(job_id)

    def get_job(self, job_id) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def list_jobs(self, domain=None, status=None, limit=50) -> list[dict]:
        sql = "SELECT * FROM jobs"
        conditions = []
        params: list = []
        if domain is not None:
            conditions.append("domain = ?")
            params.append(domain)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def update_job(self, job_id, status=None, result=None, cost=None) -> dict | None:
        if self.get_job(job_id) is None:
            return None
        sets = []
        params: list = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if result is not None:
            sets.append("result_json = ?")
            params.append(json.dumps(result))
        if cost is not None:
            sets.append("cost_json = ?")
            params.append(json.dumps(cost))
        if not sets:
            return self.get_job(job_id)
        sets.append("updated = ?")
        params.append(time.time())
        params.append(job_id)
        self._conn.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params
        )
        return self.get_job(job_id)

    def claim_next(self) -> dict | None:
        """Atomically move the oldest queued job to running, or None."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued'"
                " ORDER BY created ASC, rowid ASC LIMIT 1"
            ).fetchone()
            if row is None:
                self._conn.execute("COMMIT")
                return None
            job_id = row["id"]
            self._conn.execute(
                "UPDATE jobs SET status = 'running', updated = ?"
                " WHERE id = ? AND status = 'queued'",
                (time.time(), job_id),
            )
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return self.get_job(job_id)

    def cancel_job(self, job_id) -> dict | None:
        """Cancel a queued job; returns None unless it was queued."""
        cur = self._conn.execute(
            "UPDATE jobs SET status = 'cancelled', updated = ?"
            " WHERE id = ? AND status = 'queued'",
            (time.time(), job_id),
        )
        if cur.rowcount == 0:
            return None
        return self.get_job(job_id)
