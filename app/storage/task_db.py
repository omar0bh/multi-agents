"""SQLite-backed task store for the Cloud-to-Local PC Bridge.

The original table is preserved and migrated in place.  New columns add
leases, attempts, progress, cancellation requests and priority.  A task that
was dispatched but never reported is requeued after its lease expires, up to
``max_attempts``; the local daemon renews leases while it is still working.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "bridge_tasks.db"

_SCHEMA_COLUMNS = {
    "dispatched_at": "REAL",
    "started_at": "REAL",
    "lease_expires_at": "REAL",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL DEFAULT 3",
    "progress": "TEXT",
    "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "error": "TEXT",
    "worker": "TEXT",
    "updated_at": "REAL",
}


def _now() -> float:
    return time.time()


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def init_db() -> None:
    """Create/migrate the task table if needed (idempotent)."""
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bridge_tasks (
                task_id     TEXT PRIMARY KEY,
                action      TEXT NOT NULL,
                params      TEXT NOT NULL,
                chat_id     INTEGER,
                status      TEXT NOT NULL DEFAULT 'pending',
                result      TEXT,
                queued_at   REAL NOT NULL,
                completed_at REAL
            )
            """
        )
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(bridge_tasks)").fetchall()}
        for column, definition in _SCHEMA_COLUMNS.items():
            if column not in existing:
                try:
                    conn.execute(f"ALTER TABLE bridge_tasks ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as exc:
                    logger.debug("Migration of column %s skipped: %s", column, exc)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bridge_status ON bridge_tasks(status, priority DESC, queued_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bridge_lease ON bridge_tasks(lease_expires_at)")
        conn.commit()


def _row_to_task(row: sqlite3.Row, include_result: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task_id": row["task_id"],
        "action": row["action"],
        "params": json.loads(row["params"] or "{}"),
        "chat_id": row["chat_id"],
        "status": row["status"],
        "queued_at": row["queued_at"],
        "completed_at": row["completed_at"],
        "dispatched_at": row["dispatched_at"] if "dispatched_at" in row.keys() else None,
        "started_at": row["started_at"] if "started_at" in row.keys() else None,
        "lease_expires_at": row["lease_expires_at"] if "lease_expires_at" in row.keys() else None,
        "attempts": row["attempts"] if "attempts" in row.keys() else 0,
        "max_attempts": row["max_attempts"] if "max_attempts" in row.keys() else 3,
        "priority": row["priority"] if "priority" in row.keys() else 0,
        "cancel_requested": bool(row["cancel_requested"]) if "cancel_requested" in row.keys() else False,
        "error": row["error"] if "error" in row.keys() else None,
        "worker": row["worker"] if "worker" in row.keys() else None,
        "progress": json.loads(row["progress"]) if "progress" in row.keys() and row["progress"] else None,
        "updated_at": row["updated_at"] if "updated_at" in row.keys() else None,
    }
    if include_result:
        data["result"] = json.loads(row["result"]) if row["result"] else None
    return data


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


def db_queue_task(
    action: str,
    params: dict,
    chat_id: int | None = None,
    *,
    priority: int = 0,
    max_attempts: int = 3,
    task_id: str | None = None,
) -> str:
    task_id = task_id or str(uuid.uuid4())[:8]
    with _get_conn() as conn:
        for _ in range(5):
            try:
                conn.execute(
                    "INSERT INTO bridge_tasks "
                    "(task_id, action, params, chat_id, status, queued_at, attempts, max_attempts, priority, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (task_id, action, json.dumps(params, default=str), chat_id, "pending", _now(), 0, max(1, int(max_attempts)), int(priority), _now()),
                )
                conn.commit()
                logger.info("DB queued task %s (%s)", task_id, action)
                return task_id
            except sqlite3.IntegrityError:
                task_id = str(uuid.uuid4())[:8]
        raise RuntimeError("Could not allocate a unique task id")


def db_claim_tasks(limit: int = 20, lease_seconds: int = 90, worker: str | None = None) -> list[dict]:
    """Atomically claim pending tasks and give them a lease."""
    limit = max(1, min(int(limit), 100))
    now = _now()
    claim_id = worker or str(uuid.uuid4())[:8]
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT * FROM bridge_tasks WHERE status='pending' AND cancel_requested=0 "
                "ORDER BY priority DESC, queued_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            claimed: list[dict] = []
            for row in rows:
                cur = conn.execute(
                    "UPDATE bridge_tasks SET status='dispatched', dispatched_at=?, lease_expires_at=?, "
                    "attempts=attempts+1, worker=?, updated_at=? WHERE task_id=? AND status='pending'",
                    (now, now + max(15, int(lease_seconds)), claim_id, now, row["task_id"]),
                )
                if cur.rowcount:
                    updated = conn.execute("SELECT * FROM bridge_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
                    claimed.append(_row_to_task(updated))
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise


def db_get_pending_tasks(limit: int = 20, lease_seconds: int = 90) -> list[dict]:
    """Backward-compatible alias for claiming pending tasks."""
    return db_claim_tasks(limit=limit, lease_seconds=lease_seconds)


def db_requeue_expired(now: float | None = None) -> int:
    """Requeue tasks whose worker lease expired; fail tasks out of attempts."""
    now = now or _now()
    changed = 0
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT * FROM bridge_tasks WHERE status IN ('dispatched','running') "
                "AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
                (now,),
            ).fetchall()
            for row in rows:
                attempts = int(row["attempts"] or 0)
                max_attempts = int(row["max_attempts"] or 3)
                if row["cancel_requested"]:
                    conn.execute(
                        "UPDATE bridge_tasks SET status='cancelled', completed_at=?, updated_at=?, "
                        "lease_expires_at=NULL, error=? WHERE task_id=?",
                        (now, now, "Cancelled by user; worker lease expired before it reported.", row["task_id"]),
                    )
                    logger.info("Task %s marked cancelled after lease expiry", row["task_id"])
                elif attempts >= max_attempts:
                    conn.execute(
                        "UPDATE bridge_tasks SET status='failed', error=?, completed_at=?, updated_at=? WHERE task_id=?",
                        (
                            f"Task lease expired after {attempts} attempt(s); no result was reported.",
                            now,
                            now,
                            row["task_id"],
                        ),
                    )
                    logger.warning("Task %s failed after %d attempts (lease expired)", row["task_id"], attempts)
                else:
                    conn.execute(
                        "UPDATE bridge_tasks SET status='pending', dispatched_at=NULL, lease_expires_at=NULL, "
                        "worker=NULL, error=?, updated_at=? WHERE task_id=?",
                        ("Requeued after worker lease expired.", now, row["task_id"]),
                    )
                    logger.info("Requeued task %s after lease expiry (attempt %d/%d)", row["task_id"], attempts, max_attempts)
                changed += 1
            conn.commit()
            return changed
        except Exception:
            conn.rollback()
            raise


def db_renew_leases(task_ids: list[str], lease_seconds: int = 90) -> int:
    ids = [str(t) for t in task_ids if t]
    if not ids:
        return 0
    now = _now()
    placeholders = ",".join("?" for _ in ids)
    with _get_conn() as conn:
        cur = conn.execute(
            f"UPDATE bridge_tasks SET lease_expires_at=?, updated_at=? "
            f"WHERE task_id IN ({placeholders}) AND status IN ('dispatched','running')",
            [now + max(15, int(lease_seconds)), now, *ids],
        )
        conn.commit()
        return cur.rowcount


def db_complete_task(task_id: str, result: dict, status: str | None = None) -> bool:
    """Record a terminal result. Returns False if the task was already terminal."""
    now = _now()
    final_status = status if status in ("completed", "failed", "cancelled") else None
    if final_status is None:
        if result.get("cancelled"):
            final_status = "cancelled"
        elif result.get("ok"):
            final_status = "completed"
        else:
            final_status = "failed"
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE bridge_tasks SET status=?, result=?, completed_at=?, updated_at=?, "
            "error=?, lease_expires_at=NULL, progress=NULL "
            "WHERE task_id=? AND status NOT IN ('completed','failed','cancelled')",
            (
                final_status,
                json.dumps(result, default=str),
                now,
                now,
                None if final_status == "completed" else str(result.get("error") or "")[:4000],
                task_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def db_fail_task(task_id: str, error: str, *, retryable: bool = False) -> bool:
    now = _now()
    with _get_conn() as conn:
        row = conn.execute("SELECT attempts, max_attempts, status FROM bridge_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return False
        if retryable and int(row["attempts"] or 0) < int(row["max_attempts"] or 3):
            cur = conn.execute(
                "UPDATE bridge_tasks SET status='pending', error=?, lease_expires_at=NULL, updated_at=? WHERE task_id=?",
                (error[:4000], now, task_id),
            )
        else:
            cur = conn.execute(
                "UPDATE bridge_tasks SET status='failed', error=?, completed_at=?, updated_at=?, lease_expires_at=NULL WHERE task_id=?",
                (error[:4000], now, now, task_id),
            )
        conn.commit()
        return cur.rowcount > 0


def db_update_progress(task_id: str, progress: dict, *, status: str = "running", lease_seconds: int = 90) -> bool:
    now = _now()
    with _get_conn() as conn:
        row = conn.execute("SELECT started_at, status FROM bridge_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return False
        if row["status"] in ("completed", "failed", "cancelled"):
            return False
        started = row["started_at"] or now
        cur = conn.execute(
            "UPDATE bridge_tasks SET status=?, progress=?, started_at=?, lease_expires_at=?, updated_at=? WHERE task_id=?",
            (status, json.dumps(progress, default=str)[:200_000], started, now + max(15, int(lease_seconds)), now, task_id),
        )
        conn.commit()
        return cur.rowcount > 0


def db_request_cancel(task_id: str) -> dict[str, Any]:
    """Cancel a pending task or signal a running one to stop."""
    now = _now()
    with _get_conn() as conn:
        row = conn.execute("SELECT status FROM bridge_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"found": False, "cancelled_pending": False, "status": None}
        status = row["status"]
        if status == "pending":
            conn.execute(
                "UPDATE bridge_tasks SET status='cancelled', cancel_requested=1, completed_at=?, updated_at=? WHERE task_id=?",
                (now, now, task_id),
            )
            conn.commit()
            return {"found": True, "cancelled_pending": True, "status": "cancelled"}
        if status in ("dispatched", "running"):
            conn.execute(
                "UPDATE bridge_tasks SET cancel_requested=1, updated_at=? WHERE task_id=?",
                (now, task_id),
            )
            conn.commit()
            return {"found": True, "cancelled_pending": False, "status": status, "signalled": True}
        return {"found": True, "cancelled_pending": False, "status": status}


def db_cancel_task(task_id: str) -> bool:
    """Backward-compatible pending-only cancellation."""
    return bool(db_request_cancel(task_id).get("cancelled_pending"))


def db_cancel_requests(limit: int = 100) -> list[str]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT task_id FROM bridge_tasks WHERE cancel_requested=1 AND status IN ('dispatched','running') LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [r["task_id"] for r in rows]


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


def db_get_task(task_id: str) -> dict[str, Any] | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM bridge_tasks WHERE task_id=?", (task_id,)).fetchone()
    return _row_to_task(row, include_result=True) if row else None


def db_list_tasks(limit: int = 10, status: str | None = None, action: str | None = None) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    clauses: list[str] = []
    values: list[Any] = []
    if status:
        clauses.append("status=?")
        values.append(status)
    if action:
        clauses.append("action=?")
        values.append(action)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT task_id, action, status, queued_at, completed_at, dispatched_at, attempts, max_attempts, "
            f"priority, cancel_requested, error, progress, updated_at FROM bridge_tasks {where} "
            f"ORDER BY queued_at DESC LIMIT ?",
            [*values, limit],
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["cancel_requested"] = bool(item.get("cancel_requested"))
        item["progress"] = json.loads(item["progress"]) if item.get("progress") else None
        out.append(item)
    return out


def db_pending_count() -> int:
    with _get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM bridge_tasks WHERE status='pending'").fetchone()
    return row[0] if row else 0


def db_completed_count() -> int:
    with _get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM bridge_tasks WHERE status='completed'").fetchone()
    return row[0] if row else 0


def db_stats() -> dict[str, int]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM bridge_tasks GROUP BY status").fetchall()
    stats = {r["status"]: int(r["n"]) for r in rows}
    stats["total"] = sum(stats.values())
    stats["pending"] = stats.get("pending", 0)
    stats["dispatched"] = stats.get("dispatched", 0)
    stats["running"] = stats.get("running", 0)
    stats["completed"] = stats.get("completed", 0)
    stats["failed"] = stats.get("failed", 0)
    stats["cancelled"] = stats.get("cancelled", 0)
    return stats


# Initialise on import
init_db()
