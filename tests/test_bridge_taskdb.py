"""SQLite task store: leases, requeue, cancellation, progress and stats."""

from __future__ import annotations

import sqlite3
import time

import pytest

import app.storage.task_db as task_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(task_db, "DB_PATH", tmp_path / "bridge_tasks.db")
    task_db.init_db()
    return task_db


def test_queue_claim_priority_and_attempts(db):
    low = db.db_queue_task("echo", {"x": 1}, priority=1, max_attempts=3, task_id="low")
    high = db.db_queue_task("echo", {"x": 2}, priority=5, max_attempts=3, task_id="high")

    claimed = db.db_claim_tasks(limit=1, lease_seconds=30)
    assert [t["task_id"] for t in claimed] == ["high"]
    assert claimed[0]["status"] == "dispatched"
    assert claimed[0]["attempts"] == 1
    assert claimed[0]["lease_expires_at"] is not None
    assert claimed[0]["worker"] is not None

    remaining = db.db_claim_tasks(limit=10, lease_seconds=30)
    assert [t["task_id"] for t in remaining] == ["low"]
    assert db.db_pending_count() == 0


def test_renew_leases(db):
    db.db_queue_task("echo", {}, task_id="lease")
    claimed = db.db_claim_tasks(limit=1, lease_seconds=15)
    original = claimed[0]["lease_expires_at"]
    renewed = db.db_renew_leases(["lease"], lease_seconds=120)
    assert renewed == 1
    task = db.db_get_task("lease")
    assert task["lease_expires_at"] > original


def test_requeue_expired_requeues_then_fails(db):
    db.db_queue_task("echo", {}, max_attempts=2, task_id="retry")
    db.db_claim_tasks(limit=1, lease_seconds=10)
    assert db.db_requeue_expired(time.time() + 1000) == 1
    task = db.db_get_task("retry")
    assert task["status"] == "pending"
    assert "Requeued" in task["error"]
    assert task["attempts"] == 1

    db.db_claim_tasks(limit=1, lease_seconds=10)
    assert db.db_requeue_expired(time.time() + 1000) == 1
    task = db.db_get_task("retry")
    assert task["status"] == "failed"
    assert "lease expired after 2 attempt" in task["error"]


def test_cancel_pending_and_running(db):
    db.db_queue_task("echo", {}, task_id="pending-cancel")
    outcome = db.db_request_cancel("pending-cancel")
    assert outcome == {"found": True, "cancelled_pending": True, "status": "cancelled"}
    assert db.db_get_task("pending-cancel")["status"] == "cancelled"
    assert db.db_claim_tasks(limit=10, lease_seconds=30) == []

    db.db_queue_task("echo", {}, max_attempts=1, task_id="running-cancel")
    db.db_claim_tasks(limit=1, lease_seconds=30)
    outcome = db.db_request_cancel("running-cancel")
    assert outcome["found"] is True
    assert outcome["cancelled_pending"] is False
    assert outcome["signalled"] is True
    assert db.db_cancel_requests() == ["running-cancel"]

    assert db.db_requeue_expired(time.time() + 1000) == 1
    task = db.db_get_task("running-cancel")
    assert task["status"] == "cancelled"
    assert "Cancelled by user" in task["error"]


def test_complete_is_idempotent_and_fail_retryable(db):
    db.db_queue_task("echo", {}, max_attempts=3, task_id="done")
    db.db_claim_tasks(limit=1, lease_seconds=30)

    first = db.db_complete_task("done", {"ok": True, "value": 42})
    second = db.db_complete_task("done", {"ok": True, "value": 99})
    assert first is True
    assert second is False
    task = db.db_get_task("done")
    assert task["status"] == "completed"
    assert task["result"]["value"] == 42
    assert task["progress"] is None

    db.db_queue_task("echo", {}, max_attempts=2, task_id="fail")
    db.db_claim_tasks(limit=1, lease_seconds=30)
    retryable = db.db_fail_task("fail", "transient", retryable=True)
    assert retryable is True
    assert db.db_get_task("fail")["status"] == "pending"
    db.db_claim_tasks(limit=1, lease_seconds=30)
    terminal = db.db_fail_task("fail", "permanent", retryable=True)
    assert terminal is True
    assert db.db_get_task("fail")["status"] == "failed"


def test_progress_update_and_stats(db):
    db.db_queue_task("echo", {}, task_id="progress")
    db.db_claim_tasks(limit=1, lease_seconds=30)
    assert db.db_update_progress("progress", {"percent": 42, "message": "halfway"}, status="running") is True
    task = db.db_get_task("progress")
    assert task["status"] == "running"
    assert task["progress"]["percent"] == 42
    assert task["started_at"] is not None

    assert db.db_update_progress("progress", {"percent": 100}, status="running") is True
    db.db_complete_task("progress", {"ok": True})
    assert db.db_update_progress("progress", {"percent": 1}, status="running") is False

    db.db_queue_task("echo", {}, task_id="listed")
    listed = db.db_list_tasks(limit=10, status="pending", action="echo")
    assert any(row["task_id"] == "listed" for row in listed)
    stats = db.db_stats()
    assert stats["total"] >= 2
    assert stats["completed"] >= 1
    assert db.db_pending_count() >= 1
    assert db.db_completed_count() >= 1


def test_cancel_requests_and_get_task_missing(db):
    assert db.db_request_cancel("missing") == {"found": False, "cancelled_pending": False, "status": None}
    assert db.db_get_task("missing") is None
    assert db.db_cancel_requests() == []


def test_legacy_schema_migrates_in_place(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(task_db, "DB_PATH", path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE bridge_tasks (
                task_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                params TEXT NOT NULL,
                chat_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT,
                queued_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO bridge_tasks (task_id, action, params, chat_id, status, queued_at) VALUES (?,?,?,?,?,?)",
            ("legacy", "echo", '{"x": 1}', 7, "pending", 123.0),
        )
        conn.commit()

    task_db.init_db()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(bridge_tasks)").fetchall()}
    conn.close()
    assert {"lease_expires_at", "attempts", "progress", "cancel_requested", "priority", "worker"}.issubset(columns)

    task = task_db.db_get_task("legacy")
    assert task is not None
    assert task["action"] == "echo"
    assert task["params"] == {"x": 1}
    assert task["chat_id"] == 7
    assert task["status"] == "pending"
    assert task["attempts"] == 0
    assert task["max_attempts"] == 3
