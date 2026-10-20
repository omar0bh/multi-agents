"""
SQLite-backed task store for the Cloud-to-Local PC Bridge.

Why SQLite?
- Survives AWS server restarts / crashes (in-memory BridgeManager loses everything).
- Zero extra dependencies — sqlite3 ships with Python.
- Tasks are persisted to data/bridge_tasks.db next to the project root.
"""

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "bridge_tasks.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist yet."""
    with _get_conn() as conn:
        conn.execute("""
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
        """)
        conn.commit()


# ---------- write operations ----------

def db_queue_task(action: str, params: dict, chat_id: int | None = None) -> str:
    task_id = str(uuid.uuid4())[:8]
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO bridge_tasks (task_id, action, params, chat_id, status, queued_at) VALUES (?,?,?,?,?,?)",
            (task_id, action, json.dumps(params), chat_id, "pending", time.time()),
        )
        conn.commit()
    logger.info("DB queued task %s (%s)", task_id, action)
    return task_id


def db_complete_task(task_id: str, result: dict) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE bridge_tasks SET status='completed', result=?, completed_at=? WHERE task_id=?",
            (json.dumps(result), time.time(), task_id),
        )
        conn.commit()


def db_cancel_task(task_id: str) -> bool:
    """Cancel a pending task. Returns True if found and cancelled."""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE bridge_tasks SET status='cancelled' WHERE task_id=? AND status='pending'",
            (task_id,),
        )
        conn.commit()
        return cur.rowcount > 0


# ---------- read operations ----------

def db_get_pending_tasks() -> list[dict]:
    """Pop all pending tasks (marks them as dispatched atomically)."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bridge_tasks WHERE status='pending' ORDER BY queued_at"
        ).fetchall()
        if rows:
            ids = [r["task_id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE bridge_tasks SET status='dispatched' WHERE task_id IN ({placeholders})",
                ids,
            )
            conn.commit()
        return [
            {
                "task_id": r["task_id"],
                "action": r["action"],
                "params": json.loads(r["params"]),
                "chat_id": r["chat_id"],
                "queued_at": r["queued_at"],
            }
            for r in rows
        ]


def db_list_tasks(limit: int = 10) -> list[dict]:
    """Return recent tasks for the /tasks command."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT task_id, action, status, queued_at, completed_at FROM bridge_tasks ORDER BY queued_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def db_pending_count() -> int:
    with _get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM bridge_tasks WHERE status='pending'").fetchone()
    return row[0] if row else 0


def db_completed_count() -> int:
    with _get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM bridge_tasks WHERE status='completed'").fetchone()
    return row[0] if row else 0


# Initialise on import
init_db()
