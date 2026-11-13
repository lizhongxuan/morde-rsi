import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from service.store import JobStore


def _store(tmp_path):
    return JobStore(tmp_path / "nested" / "jobs.db")


def test_create_and_get_round_trip(tmp_path):
    store = _store(tmp_path)
    try:
        task = {"task_id": "CAND-001", "description": "fix the bug"}
        job = store.create_job("morde", repo="/tmp/repo", task_id="CAND-001",
                               task=task, fixer="api", diff="diff --git x")
        assert job["id"]
        assert len(job["id"]) == 32  # uuid4 hex
        assert job["status"] == "queued"
        assert job["domain"] == "morde"
        assert job["repo"] == "/tmp/repo"
        assert job["task_id"] == "CAND-001"
        assert job["task"] == task
        assert job["fixer"] == "api"
        assert job["diff"] == "diff --git x"
        assert job["result"] is None
        assert job["cost"] is None
        assert job["created"] <= job["updated"]
        assert set(job) == {"id", "domain", "repo", "task_id", "task",
                            "fixer", "diff", "status", "result", "cost",
                            "created", "updated"}
        assert store.get_job(job["id"]) == job
    finally:
        store.close()


def test_create_defaults(tmp_path):
    store = _store(tmp_path)
    try:
        job = store.create_job("self")
        assert job["repo"] is None
        assert job["task_id"] is None
        assert job["task"] is None
        assert job["fixer"] == "api"
        assert job["diff"] is None
        assert job["status"] == "queued"
    finally:
        store.close()


def test_get_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.get_job("nope") is None
    finally:
        store.close()


def test_list_newest_first_and_filters(tmp_path):
    store = _store(tmp_path)
    try:
        a = store.create_job("morde", task_id="A")
        b = store.create_job("self", task_id="B")
        c = store.create_job("morde", task_id="C")
        store.update_job(c["id"], status="running")
        ids = [j["id"] for j in store.list_jobs()]
        assert ids == [c["id"], b["id"], a["id"]]  # newest first
        assert [j["id"] for j in store.list_jobs(domain="morde")] == [c["id"], a["id"]]
        assert [j["id"] for j in store.list_jobs(status="running")] == [c["id"]]
        assert [j["id"] for j in store.list_jobs(domain="morde", status="queued")] == [a["id"]]
        assert len(store.list_jobs(limit=2)) == 2
    finally:
        store.close()


def test_update_job_result_cost_status(tmp_path):
    store = _store(tmp_path)
    try:
        job = store.create_job("morde", task_id="A")
        updated = store.update_job(job["id"], status="done",
                                   result={"verdict": "ADOPT", "score": 0.9},
                                   cost={"usd": 0.02, "tokens_in": 100})
        assert updated["status"] == "done"
        assert updated["result"] == {"verdict": "ADOPT", "score": 0.9}
        assert updated["cost"] == {"usd": 0.02, "tokens_in": 100}
        assert updated["updated"] >= job["updated"]
        # Persisted across a fresh read.
        assert store.get_job(job["id"])["result"] == {"verdict": "ADOPT", "score": 0.9}
        # Partial update leaves the rest alone.
        again = store.update_job(job["id"], status="archived")
        assert again["result"] == {"verdict": "ADOPT", "score": 0.9}
        assert again["status"] == "archived"
    finally:
        store.close()


def test_update_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.update_job("nope", status="done") is None
    finally:
        store.close()


def test_claim_next_oldest_first_and_atomic(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.claim_next() is None  # empty store
        first = store.create_job("morde", task_id="first")
        second = store.create_job("morde", task_id="second")
        third = store.create_job("morde", task_id="third")
        claimed1 = store.claim_next()
        claimed2 = store.claim_next()
        claimed3 = store.claim_next()
        # Oldest first, each claimed exactly once, all different.
        assert [c["task_id"] for c in (claimed1, claimed2, claimed3)] == [
            "first", "second", "third"]
        assert len({claimed1["id"], claimed2["id"], claimed3["id"]}) == 3
        assert all(c["status"] == "running" for c in (claimed1, claimed2, claimed3))
        assert store.claim_next() is None  # nothing queued left
        # Claimed jobs are no longer queued.
        assert store.list_jobs(status="queued") == []
        assert {j["id"] for j in (first, second, third)} == {
            claimed1["id"], claimed2["id"], claimed3["id"]}
    finally:
        store.close()


def test_claim_next_two_claims_different_jobs(tmp_path):
    # The contract's atomicity check: two successive claims never return
    # the same job (and a second store handle sees the same state).
    store = _store(tmp_path)
    try:
        store.create_job("morde", task_id="A")
        store.create_job("morde", task_id="B")
        one = store.claim_next()
        two = store.claim_next()
        assert one is not None and two is not None
        assert one["id"] != two["id"]
        assert store.claim_next() is None
    finally:
        store.close()


def test_cancel_rules(tmp_path):
    store = _store(tmp_path)
    try:
        queued = store.create_job("morde", task_id="q")
        cancelled = store.cancel_job(queued["id"])
        assert cancelled is not None
        assert cancelled["status"] == "cancelled"
        assert store.get_job(queued["id"])["status"] == "cancelled"
        # Cancelling again: no longer queued -> None.
        assert store.cancel_job(queued["id"]) is None
        # Running job cannot be cancelled.
        running = store.create_job("morde", task_id="r")
        store.claim_next()
        assert store.get_job(running["id"])["status"] == "running"
        assert store.cancel_job(running["id"]) is None
        assert store.get_job(running["id"])["status"] == "running"
        # Missing job -> None.
        assert store.cancel_job("nope") is None
    finally:
        store.close()
