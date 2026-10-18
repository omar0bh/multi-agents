import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Heartbeat considered offline after 45 seconds without ping
HEARTBEAT_TIMEOUT_SECONDS = 45


class BridgeManager:
    """Manages the connection state and task queue between the AWS Cloud Bot and the user's Local PC."""

    def __init__(self):
        self._last_heartbeat: float = 0.0
        self._pc_info: dict[str, Any] = {}
        self._pending_tasks: list[dict[str, Any]] = []
        self._completed_tasks: dict[str, dict[str, Any]] = {}

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
        
        # Pop pending tasks to hand over to the local PC
        tasks_to_dispatch = list(self._pending_tasks)
        self._pending_tasks.clear()
        if tasks_to_dispatch:
            logger.info("Dispatched %d pending tasks to local PC", len(tasks_to_dispatch))
        return tasks_to_dispatch

    def queue_task(self, action: str, params: dict[str, Any], chat_id: int | None = None) -> str:
        """Queue a task to be executed on the local PC."""
        task_id = str(uuid.uuid4())[:8]
        task_record = {
            "task_id": task_id,
            "action": action,
            "params": params,
            "chat_id": chat_id,
            "queued_at": time.time(),
        }
        self._pending_tasks.append(task_record)
        logger.info("Queued task %s (action=%s) for local PC", task_id, action)
        return task_id

    def complete_task(self, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Record the completion of a task dispatched to the local PC."""
        record = {
            "task_id": task_id,
            "result": result,
            "completed_at": time.time(),
        }
        self._completed_tasks[task_id] = record
        logger.info("Local PC completed task %s: ok=%s", task_id, result.get("ok", True))
        return record

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
            "pending_tasks_count": len(self._pending_tasks),
            "completed_tasks_count": len(self._completed_tasks),
            "pc_details": self._pc_info,
        }


# Global singleton instance
bridge_manager = BridgeManager()
