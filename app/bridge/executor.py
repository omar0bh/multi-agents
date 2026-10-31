"""Central executor: registry dispatch, metadata, audit, history, cancellation."""

from __future__ import annotations

import logging
import time
import traceback
import uuid
from typing import Any, Callable

from app.bridge import actions as actions_mod
from app.bridge.audit import audit, record_execution
from app.bridge.capabilities import PROTOCOL_VERSION
from app.bridge.context import ExecutionContext
from app.bridge.runner import ExecutionCancelled, cancellations
from app.bridge.security import redact, redact_obj

logger = logging.getLogger(__name__)

LegacyHandler = Callable[[str, dict[str, Any]], dict[str, Any]]


class BridgeExecutor:
    def __init__(self) -> None:
        self.legacy_handler: LegacyHandler | None = None

    def set_legacy_handler(self, handler: LegacyHandler | None) -> None:
        self.legacy_handler = handler

    def resolve(self, action: str):
        return actions_mod.actions.resolve(action)

    def manifest(self) -> list[dict[str, Any]]:
        return actions_mod.actions.manifest()

    def execute(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
        context: ExecutionContext | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Public entry point used by the daemon and local callers."""
        params = dict(params or {})
        action = str(action or "").strip()
        if not action:
            return self._failure("unknown", {}, "No action supplied")

        top_level = context is None
        if context is None:
            context = ExecutionContext(task_id=task_id, progress_callback=progress_callback)
            if task_id:
                cancellations.register(task_id, context.cancellation)
        else:
            if task_id:
                context.task_id = task_id
            if progress_callback and not context.progress_callback:
                context.progress_callback = progress_callback

        try:
            result = self._execute_inner(action, params, context, record_history=True)
        finally:
            if top_level and task_id:
                cancellations.unregister(task_id)
        return result


    def dispatch(self, action: str, params: dict[str, Any] | None, context: ExecutionContext) -> dict[str, Any]:
        """Execute a nested action (workflow step, local cloud fallback)."""
        return self._execute_inner(str(action or ""), dict(params or {}), context, record_history=False)

    # ------------------------------------------------------------------

    def _execute_inner(
        self,
        action: str,
        params: dict[str, Any],
        context: ExecutionContext,
        *,
        record_history: bool,
    ) -> dict[str, Any]:
        started = time.time()
        execution_id = context.execution_id
        task_id = context.task_id
        params = dict(params or {})
        requested_action = action
        spec = self.resolve(action)
        canonical = spec.name if spec else requested_action.lower().strip()
        risk = spec.risk if spec else "legacy"
        context.progress(percent=0, message=f"Starting {canonical}", force=True)
        audit_fields = {
            "execution_id": execution_id,
            "task_id": task_id,
            "action": canonical,
            "requested_action": requested_action,
            "params_keys": sorted(str(k) for k in params.keys()),
            "cwd": params.get("cwd") or params.get("folder") or params.get("path"),
        }
        try:
            context.check()
            if spec:
                result = spec.handler(params, context, self)
            elif self.legacy_handler is not None:
                result = self.legacy_handler(requested_action, params)
            else:
                result = {
                    "ok": False,
                    "error": f"Unknown action: {requested_action}",
                    "status": "unsupported",
                }
            if not isinstance(result, dict):
                result = {"ok": False, "error": f"Action handler returned {type(result).__name__}, expected dict"}
        except ExecutionCancelled as exc:
            result = {
                "ok": False,
                "status": "cancelled",
                "cancelled": True,
                "error": f"Cancelled: {exc}",
            }
        except Exception as exc:
            logger.exception("Bridge action failed: %s", canonical)
            result = {
                "ok": False,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            audit.record("execution_exception", action=canonical, error=str(exc), traceback=redact(traceback.format_exc(), max_chars=4000))

        if not isinstance(result, dict):
            result = {"ok": False, "error": "Invalid action result"}
        result.setdefault("ok", False)
        result.setdefault("status", "completed" if result.get("ok") else "failed")
        if result.get("blocked"):
            result["status"] = "blocked"
        if result.get("cancelled"):
            result["status"] = "cancelled"
        result.setdefault("action", canonical)
        result["requested_action"] = requested_action
        result["execution_id"] = execution_id
        result["task_id"] = task_id
        result["protocol_version"] = PROTOCOL_VERSION
        result["environment"] = "local"
        result["started_at"] = started
        result["finished_at"] = time.time()
        result["duration_ms"] = int((result["finished_at"] - started) * 1000)
        result.setdefault("risk", risk)
        result = redact_obj(result)
        # redact_obj turns sensitive-key values into placeholders; restore the
        # trusted execution envelope.
        result["execution_id"] = execution_id
        result["task_id"] = task_id
        result["action"] = canonical
        result["requested_action"] = requested_action
        result["duration_ms"] = int((time.time() - started) * 1000)

        if not result.get("ok"):
            result.pop("verified", None)
        if result.get("ok") and not result.get("verified"):
            # Commands/files include explicit evidence (returncode/sha/checks).
            result["verified"] = bool(
                result.get("returncode") == 0
                or result.get("verification", {}).get("ok")
                or result.get("sha256")
                or result.get("verified")
            )

        if record_history:
            try:
                record_execution(result)
            except Exception:
                pass

        event = "execution" if record_history else "nested_execution"
        audit.record(
            event,
            **audit_fields,
            ok=bool(result.get("ok")),
            status=result.get("status"),
            risk=result.get("risk", risk),
            duration_ms=result.get("duration_ms"),
            verified=bool(result.get("verified")),
            error=str(result.get("error", ""))[:2000],
            security_blocked=bool(result.get("blocked")),
        )
        context.progress(percent=100, message=f"Finished {canonical}: {result.get('status')}", force=True, ok=bool(result.get("ok")))
        return result

    def _failure(self, action: str, params: dict[str, Any], error: str) -> dict[str, Any]:
        now = time.time()
        return {
            "ok": False,
            "action": action,
            "status": "failed",
            "error": error,
            "execution_id": uuid.uuid4().hex[:12],
            "started_at": now,
            "finished_at": now,
            "duration_ms": 0,
            "protocol_version": PROTOCOL_VERSION,
            "environment": "local",
        }


bridge_executor = BridgeExecutor()
execute_local_task = bridge_executor.execute
