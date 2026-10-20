import logging
import time
import uuid
from typing import Any

from app.storage.task_db import (
    db_cancel_task,
    db_complete_task,
    db_completed_count,
    db_get_pending_tasks,
    db_list_tasks,
    db_pending_count,
    db_queue_task,
)

logger = logging.getLogger(__name__)

# Heartbeat considered offline after 45 seconds without ping
HEARTBEAT_TIMEOUT_SECONDS = 45


class BridgeManager:
    """Manages the connection state and task queue between the AWS Cloud Bot and the user's Local PC.

    Tasks are persisted to SQLite so they survive server restarts.
    In-memory state only tracks the live connection info (hostname, IP, etc.).
    """

    def __init__(self):
        self._last_heartbeat: float = 0.0
        self._pc_info: dict[str, Any] = {}

    @property
    def is_online(self) -> bool:
        """Return True if local PC sent a heartbeat within the timeout window."""
        if self._last_heartbeat <= 0:
            return False
        return (time.time() - self._last_heartbeat) < HEARTBEAT_TIMEOUT_SECONDS

    def record_heartbeat(self, pc_info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Record a heartbeat ping from the local PC and return any pending tasks."""
        self._last_heartbeat = time.time()
        if pc_info:
            self._pc_info = pc_info

        # Atomically pop pending tasks from DB to hand over to the local PC
        tasks = db_get_pending_tasks()
        if tasks:
            logger.info("Dispatched %d pending tasks to local PC", len(tasks))
        return tasks

    def queue_task(self, action: str, params: dict[str, Any], chat_id: int | None = None) -> str:
        """Queue a task to be executed on the local PC (persisted in SQLite)."""
        task_id = db_queue_task(action, params, chat_id)
        logger.info("Queued task %s (action=%s) for local PC", task_id, action)
        return task_id

    def complete_task(self, task_id: str, result: dict[str, Any]) -> None:
        """Record the completion of a task dispatched to the local PC."""
        db_complete_task(task_id, result)
        logger.info("Local PC completed task %s: ok=%s", task_id, result.get("ok", True))

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a pending task before it reaches the local PC."""
        cancelled = db_cancel_task(task_id)
        if cancelled:
            logger.info("Task %s cancelled by user", task_id)
        return cancelled

    def list_tasks(self, limit: int = 10) -> list[dict]:
        """Return recent tasks for display (used by /tasks command)."""
        return db_list_tasks(limit)

    def get_status(self) -> dict[str, Any]:
        """Return the current bridge health and status summary."""
        now = time.time()
        online = self.is_online
        seconds_ago = int(now - self._last_heartbeat) if self._last_heartbeat > 0 else None

        return {
            "online": online,
            "last_heartbeat_seconds_ago": seconds_ago,
            "pc_name": self._pc_info.get("hostname", "Unknown PC"),
            "os": self._pc_info.get("os", "Unknown OS"),
            "desktop_dir": self._pc_info.get("desktop_dir", "Unknown"),
            "pending_tasks_count": db_pending_count(),
            "completed_tasks_count": db_completed_count(),
            "pc_details": self._pc_info,
        }


# Global singleton instance
bridge_manager = BridgeManager()
