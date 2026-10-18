from fastapi.testclient import TestClient
from app.main import app
from app.bridge.manager import bridge_manager


def test_bridge_status_endpoint():
    client = TestClient(app)
    response = client.get("/api/bridge/status")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "bridge" in data
    assert "online" in data["bridge"]


def test_bridge_heartbeat_flow():
    client = TestClient(app)
    # Queue a test task
    task_id = bridge_manager.queue_task("echo", {"hello": "world"})

    # Send heartbeat
    heartbeat_payload = {
        "hostname": "Test-DELL",
        "os": "Windows 11",
        "desktop_dir": "C:\\Users\\DELL\\Desktop",
    }
    hb_resp = client.post("/api/bridge/heartbeat", json=heartbeat_payload)
    assert hb_resp.status_code == 200
    hb_data = hb_resp.json()
    assert hb_data["ok"] is True
    assert hb_data["online"] is True
    assert any(t["task_id"] == task_id for t in hb_data["pending_tasks"])

    # Report completion
    report_payload = {
        "task_id": task_id,
        "result": {"ok": True, "output": "Done!"},
    }
    report_resp = client.post("/api/bridge/task/report", json=report_payload)
    assert report_resp.status_code == 200
    assert report_resp.json()["ok"] is True

    # Status should now reflect online
    status_resp = client.get("/api/bridge/status")
    assert status_resp.json()["bridge"]["online"] is True
    assert status_resp.json()["bridge"]["pc_name"] == "Test-DELL"
