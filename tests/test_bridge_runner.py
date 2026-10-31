"""Tests for process execution, cancellation, managed processes and resources."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from app.bridge import runner
from app.bridge.runner import (
    CancellationToken,
    cancellations,
    clamp_timeout,
    interpreter_args,
    resolve_interpreter,
    run_process,
    truncate_output,
)


def test_clamp_timeout_bounds():
    assert clamp_timeout(None, default=10) == 10
    assert clamp_timeout("bad", default=10) == 10
    assert clamp_timeout(-5) >= 1
    assert clamp_timeout(99999, maximum=120) == 120


def test_truncate_output_preserves_head_and_tail():
    text = "a" * 5000 + "MIDDLE" + "b" * 5000
    out, truncated = truncate_output(text, 1000)
    assert truncated is True
    assert len(out) <= 1100
    assert out.startswith("a")
    assert out.endswith("b")
    assert "[truncated" in out


def test_run_process_success():
    result = run_process([sys.executable, "-c", "print('bridge-ok')"], shell=False, timeout=30)
    assert result.ok is True
    assert result.returncode == 0
    assert "bridge-ok" in result.stdout
    assert result.cancelled is False and result.timed_out is False


def test_run_process_failure_exposes_exit_code():
    result = run_process([sys.executable, "-c", "import sys; sys.exit(7)"], shell=False, timeout=30)
    assert result.ok is False
    assert result.returncode == 7
    assert "exited with code 7" in result.error


def test_run_process_timeout_kills_child():
    started = time.time()
    result = run_process([sys.executable, "-c", "import time; time.sleep(30)"], shell=False, timeout=1)
    assert result.timed_out is True
    assert result.ok is False
    assert time.time() - started < 20


def test_run_process_cancellation():
    token = CancellationToken()
    timer = threading.Timer(0.4, token.cancel, kwargs={"reason": "unit test"})
    timer.start()
    try:
        result = run_process([sys.executable, "-c", "import time; time.sleep(30)"], shell=False, timeout=30, token=token)
    finally:
        timer.join()
    assert result.cancelled is True
    assert result.ok is False
    assert "unit test" in result.error


def test_cancellation_registry():
    token = cancellations.register("unit-task-1")
    assert token is not None
    assert cancellations.get("unit-task-1") is token
    assert cancellations.active() .count("unit-task-1") >= 1
    assert cancellations.cancel("unit-task-1", "stop") is True
    assert token.cancelled is True
    cancellations.unregister("unit-task-1")
    assert cancellations.get("unit-task-1") is None


def test_repeated_cancel_is_idempotent():
    token = CancellationToken()
    token.cancel("first")
    token.cancel("second")
    assert token.cancelled is True
    assert token.reason == "first"


def test_interpreter_resolution():
    exe, ext, error = resolve_interpreter("python")
    assert exe and ext == ".py" and not error
    args = interpreter_args("python", exe, Path("job.py"), ["a"])
    assert args[0] == exe
    assert str(Path("job.py")) in args
    unsupported_exe, _, unsupported_error = resolve_interpreter("brainfuck")
    assert unsupported_exe is None
    assert "Unsupported" in unsupported_error


def test_managed_process_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROCESS_DIR", tmp_path / "processes")
    manager = runner.ProcessManager()
    proc = manager.start(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
        shell=False,
        name="unit-process",
    )
    try:
        time.sleep(0.5)
        assert proc.running() is True
        listed = manager.list()
        assert any(row["process_id"] == proc.process_id for row in listed)
        logs = manager.logs(proc.process_id)
        assert logs["ok"] is True
        assert "started" in logs["lines"]
        stopped = manager.stop(proc.process_id, timeout=10)
        assert stopped["ok"] is True
        assert stopped["stopped"] is True
        assert proc.running() is False
    finally:
        manager.stop(proc.process_id, timeout=5)


def test_managed_process_unknown_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROCESS_DIR", tmp_path / "processes")
    manager = runner.ProcessManager()
    assert manager.get("does-not-exist") is None
    assert manager.stop("does-not-exist")["ok"] is False
    assert manager.logs("does-not-exist")["ok"] is False
    assert manager.wait("does-not-exist")["ok"] is False


def test_resource_capacity_shape():
    monitor = runner.ResourceMonitor(cache_seconds=60)
    capacity = monitor.capacity()
    assert capacity["cpu_cores"] >= 1
    assert capacity["memory_gb"] >= 0
    assert "gpu_count" in capacity
    assert isinstance(capacity["gpus"], list)
