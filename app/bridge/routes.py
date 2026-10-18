import logging
from typing import Any
from fastapi import APIRouter, Body, Header, HTTPException

from app.bridge.manager import bridge_manager
from app.config import TELEGRAM_ALLOWED_USER_ID

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bridge", tags=["bridge"])


@router.get("/status")
def get_bridge_status():
    """Check whether the user's Local PC is currently online or offline."""
    return {
        "ok": True,
        "bridge": bridge_manager.get_status(),
    }


@router.post("/heartbeat")
def pc_heartbeat(payload: dict[str, Any] = Body(...)):
    """Receives periodic heartbeat ping from the local PC daemon script and returns pending tasks."""
    info = {
        "hostname": payload.get("hostname", "LocalPC"),
        "os": payload.get("os", "Windows"),
        "desktop_dir": payload.get("desktop_dir", ""),
        "ip": payload.get("ip", ""),
        "timestamp": payload.get("timestamp"),
    }
    pending_tasks = bridge_manager.record_heartbeat(info)
    return {
        "ok": True,
        "online": True,
        "pending_tasks": pending_tasks,
    }


@router.post("/task/report")
async def report_task_result(payload: dict[str, Any] = Body(...)):
    """Called by the local PC bridge daemon after completing an assigned task."""
    task_id = payload.get("task_id")
    result = payload.get("result", {})
    chat_id = payload.get("chat_id")

    if not task_id:
        raise HTTPException(status_code=400, detail="Missing task_id")

    bridge_manager.complete_task(task_id, result)

    # If the task had a Telegram chat_id, notify the user immediately!
    if chat_id:
        from app.integrations.telegram_bot import notify_owner
        action = result.get("action", "المهمة")
        status_msg = "✅ نجحت" if result.get("ok", True) else "❌ فشلت"
        details = result.get("detail") or result.get("output") or result.get("error") or ""
        text = (
            f"⚡ **تحديث من حاسوبك الشخصي (Local PC):**\n"
            f"📌 المهمة: `{action}` - {status_msg}\n"
        )
        if details:
            text += f"\nالتفاصيل:\n`{str(details)[:1500]}`"
        try:
            await notify_owner(text)
        except Exception as e:
            logger.warning("Could not send Telegram bridge notification: %s", e)

    return {"ok": True, "task_id": task_id}
