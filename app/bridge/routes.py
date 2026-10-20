import logging
from typing import Any
from fastapi import APIRouter, Body, Header, HTTPException

from app.bridge.manager import bridge_manager
from app.config import BRIDGE_SECRET

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bridge", tags=["bridge"])


def _verify_bridge_secret(x_bridge_secret: str | None) -> None:
    """Reject requests that don't carry the correct BRIDGE_SECRET header.
    If BRIDGE_SECRET is not configured on the server, auth is disabled (dev mode).
    """
    if not BRIDGE_SECRET:
        return  # auth disabled — dev/local mode
    if x_bridge_secret != BRIDGE_SECRET:
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
    """Receives periodic heartbeat ping from the local PC daemon script and returns pending tasks."""
    _verify_bridge_secret(x_bridge_secret)

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
async def report_task_result(
    payload: dict[str, Any] = Body(...),
    x_bridge_secret: str | None = Header(default=None),
):
    """Called by the local PC bridge daemon after completing an assigned task."""
    _verify_bridge_secret(x_bridge_secret)

    task_id = payload.get("task_id")
    result = payload.get("result", {})
    chat_id = payload.get("chat_id")

    if not task_id:
        raise HTTPException(status_code=400, detail="Missing task_id")

    bridge_manager.complete_task(task_id, result)

    # If the task had a Telegram chat_id, notify the user immediately!
    if chat_id:
        from app.integrations.telegram_bot import notify_owner
        action = result.get("action") or "المهمة"
        status_msg = "✅ نجحت" if result.get("ok", True) else "❌ فشلت"

        if action in ("list_dir", "ls"):
            path = result.get("path", "")
            items = result.get("items", [])
            folder_name = result.get("folder_name") or "المجلد"
            if result.get("ok"):
                if items:
                    text = (
                        f"📂 **محتويات المجلد `{folder_name}` على حاسوبك الشخصي:**\n"
                        f"📁 `{path}`\n\n"
                        + "\n".join(items)
                    )
                else:
                    text = f"📂 **المجلد `{folder_name}` على حاسوبك الشخصي موجود لكنه فارغ (0 عناصر).**\n📁 `{path}`"
            else:
                err = result.get("error") or "تعذر العثور على المجلد على حاسوبك."
                text = f"❌ {err}"

        elif action == "find_file":
            path = result.get("path", "")
            preview = result.get("preview", "")
            if result.get("ok"):
                text = f"📄 **تم العثور على الملف على حاسوبك الشخصي:**\n📁 `{path}`"
                if preview:
                    text += f"\n\n👀 **معاينة:**\n```\n{preview[:800]}\n```"
            else:
                err = result.get("error") or "لم يتم العثور على الملف."
                text = f"❌ {err}"

        else:
            details = result.get("detail") or result.get("output") or result.get("error") or ""
            text = (
                f"⚡ **تحديث من حاسوبك الشخصي (Local PC):**\n"
                f"📌 المهمة: `{action}` - {status_msg}\n"
            )
            if result.get("path"):
                text += f"\n📁 **المسار:** `{result.get('path')}`"
            if result.get("title"):
                text += f"\n🏷️ **العنصر:** {result.get('title')}"
            if details:
                text += f"\n\nالتفاصيل:\n`{str(details)[:1500]}`"

        try:
            await notify_owner(text, chat_id=chat_id)
        except Exception as e:
            logger.warning("Could not send Telegram bridge notification: %s", e)

    return {"ok": True, "task_id": task_id}
