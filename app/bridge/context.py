"""Execution context shared by actions and workflows."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.bridge.runner import CancellationToken


@dataclass
class ExecutionContext:
    task_id: str | None = None
    execution_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    progress_callback: Callable[[dict[str, Any]], None] | None = None
    depth: int = 0
    parent: "ExecutionContext | None" = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _last_progress: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if self.task_id and not self.cancellation.task_id:
            self.cancellation.task_id = self.task_id

    @property
    def cancelled(self) -> bool:
        return self.cancellation.cancelled

    def check(self) -> None:
        self.cancellation.raise_if_cancelled()

    def progress(
        self,
        percent: float | None = None,
        message: str = "",
        *,
        step_id: str | None = None,
        force: bool = False,
        **extra: Any,
    ) -> None:
        """Report progress, throttled to at most ~2 updates/second."""
        now = time.time()
        with self._lock:
            if not force and now - self._last_progress < 0.5:
                return
            self._last_progress = now
        payload: dict[str, Any] = {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "percent": None if percent is None else max(0.0, min(100.0, float(percent))),
            "message": message,
            "timestamp": now,
        }
        if step_id:
            payload["step_id"] = step_id
        if extra:
            payload.update(extra)
        if self.progress_callback:
            try:
                self.progress_callback(payload)
            except Exception:
                pass

    def child(self, label: str = "step") -> "ExecutionContext":
        child = ExecutionContext(
            task_id=self.task_id,
            cancellation=self.cancellation,
            progress_callback=self.progress_callback,
            depth=self.depth + 1,
            parent=self,
            metadata={**self.metadata, "parent_execution_id": self.execution_id, "label": label},
        )
        return child
