"""Structured audit and execution history for the bridge.

Two complementary stores:

* ``data/bridge_audit.jsonl`` - append-only security/execution events with
  rotation.  Useful for incident review and for the ``audit_tail`` action.
* ``data/bridge_history.db`` - small SQLite table used by ``history`` and by
  the daemon's idempotency cache (a completed task is never executed twice if
  its report was lost).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.bridge.security import redact, redact_obj

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = Path(os.getenv("BRIDGE_AUDIT_LOG", str(PROJECT_ROOT / "data" / "bridge_audit.jsonl")))
HISTORY_DB = Path(os.getenv("BRIDGE_HISTORY_DB", str(PROJECT_ROOT / "data" / "bridge_history.db")))
MAX_AUDIT_BYTES = int(os.getenv("BRIDGE_AUDIT_MAX_BYTES", str(5 * 1024 * 1024)))

_lock = threading.RLock()


class AuditLogger:
    def __init__(self, path: Path = AUDIT_PATH, max_bytes: int = MAX_AUDIT_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = _lock

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                rotated = self.path.with_suffix(self.path.suffix + ".1")
                if rotated.exists():
                    rotated.unlink(missing_ok=True)
                self.path.replace(rotated)
        except OSError:
            pass

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        payload = {
            "ts": time.time(),
            "event": event,
            **redact_obj(fields),
        }
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception as exc:  # auditing must never crash execution
                logger.debug("Audit write failed: %s", exc)
        return payload

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()[-limit:]
            except FileNotFoundError:
                return []
            except Exception:
                return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


audit = AuditLogger()


# ---------------------------------------------------------------------------
# Local execution history / idempotency cache
# ---------------------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    HISTORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(HISTORY_DB), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_history() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_history (
                execution_id TEXT PRIMARY KEY,
                task_id      TEXT,
                action       TEXT NOT NULL,
                ok           INTEGER NOT NULL,
                status       TEXT NOT NULL,
                risk         TEXT,
                target       TEXT,
                started_at   REAL NOT NULL,
                finished_at  REAL,
                duration_ms  INTEGER,
                result_json  TEXT,
                error        TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_task ON execution_history(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_started ON execution_history(started_at DESC)")
        conn.commit()


def record_execution(result: dict[str, Any]) -> None:
    try:
        init_history()
        with _conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO execution_history
                (execution_id, task_id, action, ok, status, risk, target, started_at,
                 finished_at, duration_ms, result_json, error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.get("execution_id"),
                    result.get("task_id"),
                    result.get("action", "unknown"),
                    1 if result.get("ok") else 0,
                    result.get("status", "completed" if result.get("ok") else "failed"),
                    result.get("risk", "low"),
                    result.get("path") or result.get("target") or result.get("cwd"),
                    result.get("started_at", time.time()),
                    result.get("finished_at"),
                    result.get("duration_ms"),
                    json.dumps(redact_obj(result), ensure_ascii=False, default=str)[:200_000],
                    redact(result.get("error", ""), max_chars=5000),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("History write failed: %s", exc)


def get_task_result(task_id: str) -> dict[str, Any] | None:
    """Return the cached result for a task id (idempotency across report loss)."""
    if not task_id:
        return None
    try:
        init_history()
        with _conn() as conn:
            row = conn.execute(
                "SELECT result_json FROM execution_history WHERE task_id=? ORDER BY finished_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row and row["result_json"]:
            return json.loads(row["result_json"])
    except Exception:
        return None
    return None


def list_executions(limit: int = 20, action: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    try:
        init_history()
        with _conn() as conn:
            if action:
                rows = conn.execute(
                    "SELECT execution_id, task_id, action, ok, status, risk, target, started_at, finished_at, duration_ms, error "
                    "FROM execution_history WHERE action=? ORDER BY started_at DESC LIMIT ?",
                    (action, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT execution_id, task_id, action, ok, status, risk, target, started_at, finished_at, duration_ms, error "
                    "FROM execution_history ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def history_stats() -> dict[str, Any]:
    try:
        init_history()
        with _conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM execution_history").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM execution_history WHERE ok=0").fetchone()[0]
            last = conn.execute("SELECT MAX(started_at) FROM execution_history").fetchone()[0]
        return {"total": total, "failed": failed, "last_execution_at": last}
    except Exception:
        return {"total": 0, "failed": 0, "last_execution_at": None}


init_history()
