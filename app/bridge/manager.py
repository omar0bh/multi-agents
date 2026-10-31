"""Bridge connection state and task queue.

The live connection (hostname, IP, capabilities) is kept in memory, while every
task is persisted in SQLite.  Tasks are handed out with a lease; the worker
renews the lease through heartbeats/progress updates.  If a worker disappears,
the lease expires and the task is requeued (or failed after ``max_attempts``).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.storage.task_db import (
    db_cancel_requests,
    db_claim_tasks,
    db_complete_task,
    db_completed_count,
    db_get_task,
    db_list_tasks,
    db_pending_count,
    db_queue_task,
    db_requeue_expired,
    db_request_cancel,
    db_renew_leases,
    db_stats,
    db_update_progress,
)

logger = logging.getLogger(__name__)

# Heartbeat considered offline after 45 seconds without ping
HEARTBEAT_TIMEOUT_SECONDS = 45
DEFAULT_LEASE_SECONDS = 90
DEFAULT_CLAIM_LIMIT = 20
REQUEUE_INTERVAL_SECONDS = 10


class BridgeManager:
    """Manages the connection state and task queue between cloud and local PC."""

    def __init__(self) -> None:
        self._last_heartbeat: float = 0.0
        self._pc_info: dict[str, Any] = {}
        self._last_requeue: float = 0.0
        self._heartbeat_count = 0
        self._worker: str | None = None
        self._started_at = time.time()
        self._last_progress_at: float = 0.0
        self._lock = threading.RLock()

    @property
    def is_online(self) -> bool:
        if self._last_heartbeat <= 0:
            return False
        return (time.time() - self._last_heartbeat) < HEARTBEAT_TIMEOUT_SECONDS

    # ------------------------------------------------------------------
    # Heartbeat / queue
    # ------------------------------------------------------------------

    def _requeue_if_due(self) -> int:
        now = time.time()
        with self._lock:
            if now - self._last_requeue < REQUEUE_INTERVAL_SECONDS:
                return 0
            self._last_requeue = now
        try:
            changed = db_requeue_expired(now)
            if changed:
                logger.info("Requeued/failed %d task(s) with expired leases", changed)
            return changed
        except Exception as exc:
            logger.warning("Could not requeue expired tasks: %s", exc)
            return 0

    def heartbeat(
        self,
        pc_info: dict[str, Any] | None = None,
        *,
        worker: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        limit: int = DEFAULT_CLAIM_LIMIT,
        renew_task_ids: list[str] | None = None,
        claim_tasks: bool = True,
    ) -> dict[str, Any]:
        """Record a heartbeat, renew running leases and optionally claim work."""
        self._last_heartbeat = time.time()
        with self._lock:
            self._heartbeat_count += 1
            if worker:
                self._worker = worker
            if pc_info:
                self._pc_info = {**self._pc_info, **pc_info}
        requeued = self._requeue_if_due()

        renewed = 0
        if renew_task_ids:
            try:
                renewed = db_renew_leases(list(renew_task_ids), lease_seconds=lease_seconds)
            except Exception as exc:
                logger.warning("Could not renew leases: %s", exc)

        tasks: list[dict[str, Any]] = []
        if claim_tasks:
            try:
                tasks = db_claim_tasks(limit=limit, lease_seconds=lease_seconds, worker=worker)
            except Exception as exc:
                logger.exception("Could not claim pending tasks: %s", exc)
        if tasks:
            logger.info("Dispatched %d pending task(s) to local PC", len(tasks))

        try:
            cancel_ids = db_cancel_requests()
        except Exception as exc:
            logger.warning("Could not read cancellation requests: %s", exc)
            cancel_ids = []

        return {
            "tasks": tasks,
            "cancel_task_ids": cancel_ids,
            "renewed": renewed,
            "requeued": requeued,
            "server_time": time.time(),
        }

    def record_heartbeat(self, pc_info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Backward-compatible helper returning only the claimed tasks."""
        return self.heartbeat(pc_info)["tasks"]

    def queue_task(
        self,
        action: str,
        params: dict[str, Any],
        chat_id: int | None = None,
        *,
        priority: int = 0,
        max_attempts: int = 3,
        task_id: str | None = None,
    ) -> str:
        queued = db_queue_task(
            action,
            params,
            chat_id,
            priority=priority,
            max_attempts=max_attempts,
            task_id=task_id,
        )
        logger.info("Queued task %s (action=%s) for local PC", queued, action)
        return queued

    # ------------------------------------------------------------------
    # Task state
    # ------------------------------------------------------------------

    def complete_task(self, task_id: str, result: dict[str, Any]) -> bool:
        """Record the terminal result. Returns True only for the first report."""
        first = db_complete_task(task_id, result)
        if first:
            logger.info(
                "Local PC finished task %s: ok=%s status=%s",
                task_id,
                result.get("ok", True),
                result.get("status"),
            )
        else:
            logger.info("Ignoring duplicate completion report for task %s", task_id)
        return first

    def cancel_task(self, task_id: str) -> bool:
        """Backward-compatible boolean cancellation (pending tasks only)."""
        return bool(self.request_cancel(task_id).get("cancelled_pending"))

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        """Cancel a pending task or signal a running worker to stop."""
        outcome = db_request_cancel(task_id)
        if outcome.get("found"):
            logger.info("Cancellation requested for task %s: %s", task_id, outcome)
        return outcome

    def update_progress(self, task_id: str, progress: dict[str, Any]) -> bool:
        ok = db_update_progress(task_id, progress)
        if ok:
            with self._lock:
                self._last_progress_at = time.time()
        return ok

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return db_get_task(task_id)

    def list_tasks(
        self,
        limit: int = 10,
        *,
        status: str | None = None,
        action: str | None = None,
    ) -> list[dict]:
        return db_list_tasks(limit, status=status, action=action)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            heartbeat = self._last_heartbeat
            info = dict(self._pc_info)
            worker = self._worker
            heartbeat_count = self._heartbeat_count
            last_progress = self._last_progress_at
        seconds_ago = int(now - heartbeat) if heartbeat > 0 else None

        capabilities = info.get("capabilities") if isinstance(info.get("capabilities"), dict) else {}
        stats = db_stats()
        return {
            "online": self.is_online,
            "last_heartbeat_seconds_ago": seconds_ago,
            "pc_name": info.get("hostname", "Unknown PC"),
            "os": info.get("os", "Unknown OS"),
            "desktop_dir": info.get("desktop_dir", "Unknown"),
            "pending_tasks_count": db_pending_count(),
            "completed_tasks_count": db_completed_count(),
            "pc_details": info,
            "worker": worker,
            "heartbeat_count": heartbeat_count,
            "last_progress_seconds_ago": int(now - last_progress) if last_progress else None,
            "stats": stats,
            "capabilities": {
                "protocol_version": capabilities.get("protocol_version"),
                "bridge_version": capabilities.get("bridge_version"),
                "action_count": capabilities.get("action_count"),
                "features": capabilities.get("features", []),
            },
            "uptime_seconds": int(now - self._started_at),
        }

    def stats(self) -> dict[str, Any]:
        return db_stats()


# Global singleton instance
bridge_manager = BridgeManager()
