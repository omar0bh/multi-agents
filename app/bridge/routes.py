"""FastAPI relay endpoints used by the local bridge daemon.

The daemon polls ``/heartbeat`` (which leases work), streams ``/task/progress``
updates, reports terminal results to ``/task/report`` and receives cancellation
requests in the heartbeat response.  All state lives in SQLite via
``BridgeManager`` so a server restart does not lose queued work.
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query

from app.bridge.manager import bridge_manager
from app.config import BRIDGE_SECRET

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bridge", tags=["bridge"])


def _verify_bridge_secret(x_bridge_secret: str | None) -> None:
    """Reject requests without the configured BRIDGE_SECRET.

    Auth is disabled when BRIDGE_SECRET is empty (local development mode).
    Comparison is constant-time to avoid leaking the secret through timing.
    """
    if not BRIDGE_SECRET:
        return
    if not x_bridge_secret or not hmac.compare_digest(str(x_bridge_secret), str(BRIDGE_SECRET)):
        logger.warning("Bridge request rejected: invalid or missing X-Bridge-Secret")
        raise HTTPException(status_code=401, detail="Invalid bridge secret token.")


@router.get("/status")
def get_bridge_status():
    """Check whether the user's Local PC is currently online or offline."""
    return {
        "ok": True,
        "bridge": bridge_manager.get_status(),
    }


@router.post("/heartbeat")
def pc_heartbeat(
    payload: dict[str, Any] = Body(...),
    x_bridge_secret: str | None = Header(default=None),
):
    """Receive a heartbeat, renew leases and hand out pending tasks."""
    _verify_bridge_secret(x_bridge_secret)

    worker = payload.get("worker") or payload.get("worker_id")
    info = {
        "hostname": payload.get("hostname", "LocalPC"),
        "os": payload.get("os", "Windows"),
        "desktop_dir": payload.get("desktop_dir", ""),
        "ip": payload.get("ip", ""),
        "user": payload.get("user", ""),
        "timestamp": payload.get("timestamp"),
        "worker": worker,
        "capabilities": payload.get("capabilities") or payload.get("bridge_capabilities") or {},
        "active_tasks": payload.get("active_tasks") or payload.get("renew_task_ids") or [],
        "protocol_version": payload.get("protocol_version"),
    }

    lease_seconds = payload.get("lease_seconds", 90)
    try:
        lease_seconds = max(15, min(int(lease_seconds), 600))
    except (TypeError, ValueError):
        lease_seconds = 90
    limit = payload.get("limit", 20)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20

    renew_ids = payload.get("renew_task_ids") or payload.get("active_task_ids") or []
    if not isinstance(renew_ids, list):
        renew_ids = []

    outcome = bridge_manager.heartbeat(
        info,
        worker=str(worker) if worker else None,
        lease_seconds=lease_seconds,
        limit=limit,
        renew_task_ids=[str(t) for t in renew_ids],
        claim_tasks=bool(payload.get("claim_tasks", True)),
    )
    return {
        "ok": True,
        "online": True,
        "pending_tasks": outcome["tasks"],
        "cancel_task_ids": outcome["cancel_task_ids"],
        "renewed": outcome["renewed"],
        "requeued": outcome["requeued"],
        "server_time": outcome["server_time"],
        "heartbeat_interval_seconds": payload.get("heartbeat_interval_seconds", 10),
        "lease_seconds": lease_seconds,
    }


@router.post("/task/progress")
async def report_task_progress(
    payload: dict[str, Any] = Body(...),
    x_bridge_secret: str | None = Header(default=None),
):
    """Receive a progress update for a running task."""
    _verify_bridge_secret(x_bridge_secret)
    task_id = payload.get("task_id")
    if not task_id:
        raise HTTPException(status_code=400, detail="Missing task_id")
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        progress = {
            key: payload.get(key)
            for key in ("percent", "message", "step_id", "execution_id", "timestamp", "ok")
            if payload.get(key) is not None
        }
    progress.setdefault("timestamp", payload.get("timestamp"))
    progress["server_received_at"] = time.time()
    accepted = bridge_manager.update_progress(str(task_id), progress)
    return {"ok": True, "task_id": task_id, "accepted": accepted}


@router.post("/task/report")
async def report_task_result(
    payload: dict[str, Any] = Body(...),
    x_bridge_secret: str | None = Header(default=None),
):
    """Called by the local bridge daemon after completing an assigned task."""
    _verify_bridge_secret(x_bridge_secret)

    task_id = payload.get("task_id")
    result = payload.get("result") or {}
    if not task_id:
        raise HTTPException(status_code=400, detail="Missing task_id")
    if not isinstance(result, dict):
        result = {"ok": False, "error": "Invalid result payload"}

    task = bridge_manager.get_task(str(task_id)) or {}
    chat_id = payload.get("chat_id") or task.get("chat_id")
    action = result.get("action") or result.get("requested_action") or task.get("action") or "task"
    result.setdefault("action", action)

    first_report = bridge_manager.complete_task(str(task_id), result)

    notified = False
    # Only notify on the first terminal report; retries and duplicate worker
    # reports must not spam the user.
    if first_report and chat_id and not payload.get("silent"):
        text = _notification_text(action, result)
        if text:
            try:
                from app.integrations.telegram_bot import notify_owner

                await notify_owner(text, chat_id=chat_id)
                notified = True
            except Exception as exc:
                logger.warning("Could not send Telegram bridge notification: %s", exc)

    return {
        "ok": True,
        "task_id": task_id,
        "duplicate": not first_report,
        "notified": notified,
        "known": bool(task),
        "status": result.get("status"),
        "result_ok": bool(result.get("ok")),
    }


@router.get("/tasks")
def list_bridge_tasks(
    limit: int = Query(default=20, ge=1, le=500),
    status: str | None = Query(default=None),
    action: str | None = Query(default=None),
    x_bridge_secret: str | None = Header(default=None),
):
    """List recent tasks (used by operators, /tasks commands and dashboards)."""
    _verify_bridge_secret(x_bridge_secret)
    tasks = bridge_manager.list_tasks(limit, status=status, action=action)
    return {"ok": True, "tasks": tasks, "count": len(tasks), "stats": bridge_manager.stats()}


@router.get("/task/{task_id}")
def get_bridge_task(
    task_id: str,
    x_bridge_secret: str | None = Header(default=None),
):
    """Return one task including its result, progress and error."""
    _verify_bridge_secret(x_bridge_secret)
    task = bridge_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True, "task": task}


@router.post("/task/{task_id}/cancel")
def cancel_bridge_task(
    task_id: str,
    x_bridge_secret: str | None = Header(default=None),
):
    """Cancel a pending task or ask a running worker to stop."""
    _verify_bridge_secret(x_bridge_secret)
    outcome = bridge_manager.request_cancel(task_id)
    if not outcome.get("found"):
        raise HTTPException(status_code=404, detail="Task not found")
    outcome.update({"ok": True, "task_id": task_id})
    return outcome


@router.post("/task/cancel")
def cancel_bridge_task_body(
    payload: dict[str, Any] = Body(...),
    x_bridge_secret: str | None = Header(default=None),
):
    """Body-based cancellation alias (useful for simple clients)."""
    _verify_bridge_secret(x_bridge_secret)
    task_id = payload.get("task_id")
    if not task_id:
        raise HTTPException(status_code=400, detail="Missing task_id")
    outcome = bridge_manager.request_cancel(str(task_id))
    if not outcome.get("found"):
        raise HTTPException(status_code=404, detail="Task not found")
    outcome.update({"ok": True, "task_id": task_id})
    return outcome


@router.get("/capabilities")
def bridge_capabilities_endpoint(
    include_actions: bool = Query(default=True),
    include_resources: bool = Query(default=True),
    check_cloud: bool = Query(default=False),
    x_bridge_secret: str | None = Header(default=None),
):
    """Return the local daemon capability manifest (server relay view)."""
    _verify_bridge_secret(x_bridge_secret)
    from app.bridge.capabilities import bridge_capabilities

    info = bridge_capabilities(include_actions=include_actions, include_resources=include_resources)
    if check_cloud:
        from app.bridge.cloud import cloud_providers_info

        info["cloud"] = cloud_providers_info(check_availability=True)
    return {"ok": True, "capabilities": info}


# ---------------------------------------------------------------------------
# Telegram notification formatting
# ---------------------------------------------------------------------------


def _notification_text(action: str, result: dict[str, Any]) -> str:
    status_msg = "✅ نجحت" if result.get("ok", True) else "❌ فشلت"

    if action in ("list_dir", "ls"):
        path = result.get("path", "")
        items = result.get("items", [])
        folder_name = result.get("folder_name") or "المجلد"
        if result.get("ok"):
            if items:
                return (
                    f"📂 **محتويات المجلد `{folder_name}` على حاسوبك الشخصي:**\n"
                    f"📁 `{path}`\n\n"
                    + "\n".join(str(item) for item in items)
                )
            return f"📂 **المجلد `{folder_name}` على حاسوبك الشخصي موجود لكنه فارغ (0 عناصر).**\n📁 `{path}`"
        return f"❌ {result.get('error') or 'تعذر العثور على المجلد على حاسوبك.'}"

    if action in ("find_file", "read_file"):
        path = result.get("path", "")
        preview = result.get("preview", "")
        if result.get("ok"):
            text = f"📄 **تم العثور على الملف على حاسوبك الشخصي:**\n📁 `{path}`"
            if preview:
                text += f"\n\n👀 **معاينة:**\n```\n{str(preview)[:800]}\n```"
            return text
        return f"❌ {result.get('error') or 'لم يتم العثور على الملف.'}"

    if action == "workflow":
        report = result.get("report") or result.get("detail")
        if report:
            return f"🧩 **Workflow `{result.get('workflow_id', '')}` — {status_msg}**\n{str(report)[:2000]}"

    if action in ("run_command", "run_code", "package_install", "git", "cloud_execute"):
        if result.get("ok"):
            detail = result.get("stdout") or result.get("detail") or result.get("output") or "تم التنفيذ."
            return (
                f"⚡ **تم تنفيذ `{action}` على حاسوبك الشخصي**\n"
                f"⏱️ {result.get('duration_ms', 0)} ms\n```\n{str(detail)[:1800]}\n```"
            )
        return f"❌ **خطأ على حاسوبك الشخصي (`{action}`):**\n{str(result.get('error') or 'فشلت العملية.')[:1500]}"

    if action in (
        "copy_file",
        "create_folder",
        "move_file",
        "create_file",
        "delete_path",
        "mkdir",
        "write_file",
        "append_file",
        "download_file",
        "system_cleanup_and_report",
        "pc_performance_cleanup",
        "performance_report",
        "screenshot_analyze",
        "arrange_desktop",
        "get_icons",
    ):
        detail = result.get("detail") or result.get("output") or ""
        if result.get("ok"):
            return str(detail) or f"{status_msg}: `{action}`"
        return f"❌ **خطأ على حاسوبك الشخصي:**\n{result.get('error') or 'فشلت العملية.'}"

    details = result.get("detail") or result.get("output") or result.get("stdout") or result.get("error") or ""
    text = f"⚡ **تحديث من حاسوبك الشخصي (Local PC):**\n📌 المهمة: `{action}` - {status_msg}\n"
    if result.get("path"):
        text += f"\n📁 **المسار:** `{result.get('path')}`"
    if result.get("title"):
        text += f"\n🏷️ **العنصر:** {result.get('title')}"
    if details:
        text += f"\n\nالتفاصيل:\n`{str(details)[:1500]}`"
    return text
