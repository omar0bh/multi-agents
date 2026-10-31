"""FastAPI bridge relay endpoints added by the upgrade."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage import task_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(task_db, "DB_PATH", tmp_path / "api_tasks.db")
    task_db.init_db()
    monkeypatch.setenv("BRIDGE_SECRET", "api-secret")
    import app.bridge.routes as routes

    monkeypatch.setattr(routes, "BRIDGE_SECRET", "api-secret")
    return TestClient(app)


HEADERS = {"X-Bridge-Secret": "api-secret"}


def test_auth_required(client):
    assert client.get("/api/bridge/tasks").status_code == 401
    assert client.get("/api/bridge/tasks", headers={"X-Bridge-Secret": "wrong"}).status_code == 401
    response = client.get("/api/bridge/tasks", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_heartbeat_claims_tasks_and_renews_leases(client):
    task_db.db_queue_task("echo", {"x": 1}, priority=5, task_id="high")
    task_db.db_queue_task("echo", {"x": 2}, priority=1, task_id="low")

    response = client.post(
        "/api/bridge/heartbeat",
        json={"hostname": "Test-PC", "os": "Windows", "limit": 1, "claim_tasks": True, "lease_seconds": 60},
        headers=HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["online"] is True
    assert [t["task_id"] for t in body["pending_tasks"]] == ["high"]
    assert body["renewed"] == 0

    # Renew the claimed task's lease through a subsequent heartbeat.
    renew = client.post(
        "/api/bridge/heartbeat",
        json={"hostname": "Test-PC", "renew_task_ids": ["high"], "claim_tasks": False, "lease_seconds": 120},
        headers=HEADERS,
    )
    assert renew.status_code == 200
    assert renew.json()["renewed"] == 1

    no_claim = client.post(
        "/api/bridge/heartbeat",
        json={"hostname": "Test-PC", "claim_tasks": False},
        headers=HEADERS,
    )
    assert no_claim.json()["pending_tasks"] == []


def test_progress_endpoint(client):
    task_db.db_queue_task("echo", {}, task_id="progress")
    task_db.db_claim_tasks(limit=1, lease_seconds=30)

    missing = client.post("/api/bridge/task/progress", json={}, headers=HEADERS)
    assert missing.status_code == 400

    response = client.post(
        "/api/bridge/task/progress",
        json={"task_id": "progress", "percent": 50, "message": "halfway", "step_id": "a"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert task_db.db_get_task("progress")["progress"]["percent"] == 50


def test_task_report_idempotent_and_notification(client):
    task_db.db_queue_task("echo", {}, chat_id=123, task_id="reported")
    task_db.db_claim_tasks(limit=1, lease_seconds=30)

    notifications: list[tuple[str, int]] = []

    async def fake_notify(text: str, chat_id: int | None = None, **kwargs):
        notifications.append((text, chat_id))
        return {"ok": True}

    with patch("app.integrations.telegram_bot.notify_owner", fake_notify):
        first = client.post(
            "/api/bridge/task/report",
            json={"task_id": "reported", "result": {"ok": True, "output": "done"}, "chat_id": 123},
            headers=HEADERS,
        )
        assert first.status_code == 200
        body = first.json()
        assert body["ok"] is True
        assert body["duplicate"] is False
        assert body["notified"] is True

        second = client.post(
            "/api/bridge/task/report",
            json={"task_id": "reported", "result": {"ok": True, "output": "done again"}},
            headers=HEADERS,
        )
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert second.json()["notified"] is False

    assert len(notifications) == 1

    # Silent reports do not notify.
    task_db.db_queue_task("echo", {}, chat_id=999, task_id="silent")
    task_db.db_claim_tasks(limit=1, lease_seconds=30)
    with patch("app.integrations.telegram_bot.notify_owner", fake_notify):
        silent = client.post(
            "/api/bridge/task/report",
            json={"task_id": "silent", "result": {"ok": True}, "silent": True},
            headers=HEADERS,
        )
    assert silent.json()["notified"] is False


def test_task_get_list_cancel_and_status(client):
    task_db.db_queue_task("echo", {"x": 1}, task_id="pending-cancel")
    task_db.db_queue_task("echo", {"x": 2}, task_id="done-task")

    # Cancel a pending task.
    cancel = client.post("/api/bridge/task/pending-cancel/cancel", headers=HEADERS)
    assert cancel.status_code == 200
    assert cancel.json()["cancelled_pending"] is True

    # Body-based cancellation alias.
    task_db.db_queue_task("echo", {}, task_id="body-cancel")
    body_cancel = client.post("/api/bridge/task/cancel", json={"task_id": "body-cancel"}, headers=HEADERS)
    assert body_cancel.status_code == 200
    assert body_cancel.json()["cancelled_pending"] is True

    missing_cancel = client.post("/api/bridge/task/does-not-exist/cancel", headers=HEADERS)
    assert missing_cancel.status_code == 404

    missing_body = client.post("/api/bridge/task/cancel", json={}, headers=HEADERS)
    assert missing_body.status_code == 400

    # Report completion for done-task.
    task_db.db_claim_tasks(limit=10, lease_seconds=30)
    report = client.post(
        "/api/bridge/task/report",
        json={"task_id": "done-task", "result": {"ok": True, "value": 7}},
        headers=HEADERS,
    )
    assert report.status_code == 200

    fetched = client.get("/api/bridge/task/done-task", headers=HEADERS)
    assert fetched.status_code == 200
    assert fetched.json()["task"]["task_id"] == "done-task"
    assert fetched.json()["task"]["result"]["value"] == 7

    missing_task = client.get("/api/bridge/task/nope", headers=HEADERS)
    assert missing_task.status_code == 404

    listed = client.get("/api/bridge/tasks", params={"limit": 5, "status": "cancelled", "action": "echo"}, headers=HEADERS)
    assert listed.status_code == 200
    assert listed.json()["count"] >= 1
    assert all(task["status"] == "cancelled" for task in listed.json()["tasks"])

    status = client.get("/api/bridge/status")
    assert status.status_code == 200
    assert "bridge" in status.json()
    assert "online" in status.json()["bridge"]


def test_capabilities_endpoint(client):
    response = client.get("/api/bridge/capabilities", params={"include_actions": True, "check_cloud": True}, headers=HEADERS)
    assert response.status_code == 200
    caps = response.json()["capabilities"]
    assert caps["protocol_version"] == "2.0"
    assert "protected_path_enforcement" in caps["features"]
    assert caps["action_count"] > 0
    assert "cloud" in caps

    # Missing task id on report/progress is rejected.
    assert client.post("/api/bridge/task/report", json={"result": {}}, headers=HEADERS).status_code == 400
    assert client.post("/api/bridge/task/progress", json={}, headers=HEADERS).status_code == 400


def test_cancel_running_task_signals_and_appears_in_heartbeat(client):
    task_db.db_queue_task("echo", {}, task_id="running")
    task_db.db_claim_tasks(limit=1, lease_seconds=60)

    cancel = client.post("/api/bridge/task/running/cancel", headers=HEADERS)
    assert cancel.status_code == 200
    assert cancel.json()["signalled"] is True

    hb = client.post("/api/bridge/heartbeat", json={"hostname": "Test-PC", "claim_tasks": False}, headers=HEADERS)
    assert "running" in hb.json()["cancel_task_ids"]

    # Reporting a cancelled result stores cancelled status.
    report = client.post(
        "/api/bridge/task/report",
        json={"task_id": "running", "result": {"ok": False, "cancelled": True, "status": "cancelled"}},
        headers=HEADERS,
    )
    assert report.status_code == 200
    assert task_db.db_get_task("running")["status"] == "cancelled"
