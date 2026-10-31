"""Workflow orchestration tests: DAGs, parallelism, retries, conditions,
verification, cancellation and checkpoint/resume."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.bridge.context import ExecutionContext
from app.bridge.workflow import WorkflowError, run_workflow, verify_result


def execute_factory(calls, fn):
    def execute(action: str, params: dict, ctx: ExecutionContext) -> dict:
        calls.append((action, params, ctx.depth))
        return fn(action, params, ctx)

    return execute


def test_template_resolution_and_dag_order(tmp_path):
    calls: list[tuple[str, dict, int]] = []
    state = {}

    def fn(action, params, ctx):
        if action == "echo":
            return {"ok": True, "echo": {"value": params.get("value")}, "stdout": str(params.get("value"))}
        if action == "combine":
            state["combined"] = params.get("left")
            return {"ok": True, "combined": params.get("left")}
        return {"ok": False, "error": "unknown"}

    execute = execute_factory(calls, fn)
    spec = {
        "workflow_id": "tmpl",
        "steps": [
            {"id": "first", "action": "echo", "params": {"value": 7}},
            {
                "id": "second",
                "action": "combine",
                "depends_on": ["first"],
                "params": {"left": "{{ steps.first.stdout }}-done"},
            },
        ],
    }
    result = run_workflow(spec, execute=execute, context=ExecutionContext(), checkpoint_dir=tmp_path)
    assert result["ok"] is True
    assert result["status"] == "succeeded"
    assert result["steps"]["first"]["ok"] is True
    assert result["steps"]["second"]["combined"] == "7-done"
    assert [call[0] for call in calls] == ["echo", "combine"]


def test_parallel_branches_run_concurrently(tmp_path):
    barrier = threading.Barrier(2, timeout=5)
    calls: list[str] = []

    def fn(action, params, ctx):
        barrier.wait()
        calls.append(params["label"])
        return {"ok": True, "label": params["label"], "status": "completed"}

    execute = execute_factory([], fn)
    spec = {
        "workflow_id": "parallel",
        "max_parallel": 2,
        "steps": [
            {"id": "a", "action": "work", "params": {"label": "A"}},
            {"id": "b", "action": "work", "params": {"label": "B"}},
        ],
    }
    result = run_workflow(spec, execute=execute, context=ExecutionContext(), checkpoint_dir=tmp_path)
    assert result["ok"] is True
    assert result["succeeded_steps"] == ["a", "b"]
    assert set(calls) == {"A", "B"}


def test_retries_eventually_succeed(tmp_path):
    attempts_by_step: dict[str, int] = {}

    def fn(action, params, ctx):
        attempts_by_step["work"] = attempts_by_step.get("work", 0) + 1
        if attempts_by_step["work"] < 3:
            return {"ok": False, "error": "transient"}
        return {"ok": True, "attempt": attempts_by_step["work"], "status": "completed"}

    execute = execute_factory([], fn)
    spec = {
        "workflow_id": "retry",
        "steps": [
            {"id": "work", "action": "work", "retries": 2, "retry_delay": 0, "params": {}},
        ],
    }
    result = run_workflow(spec, execute=execute, context=ExecutionContext(), checkpoint_dir=tmp_path)
    assert result["ok"] is True
    assert result["steps"]["work"]["attempt"] == 3
    assert attempts_by_step["work"] == 3


def test_false_when_skips_benignly(tmp_path):
    calls: list[str] = []

    def fn(action, params, ctx):
        calls.append(params["id"])
        return {"ok": True, "status": "completed"}

    execute = execute_factory([], fn)
    spec = {
        "workflow_id": "when",
        "steps": [
            {"id": "a", "action": "work", "params": {"id": "A"}},
            {"id": "b", "action": "work", "when": False, "params": {"id": "B"}},
            {"id": "c", "action": "work", "depends_on": ["b"], "params": {"id": "C"}},
        ],
    }
    result = run_workflow(spec, execute=execute, context=ExecutionContext(), checkpoint_dir=tmp_path)
    assert result["ok"] is True
    assert result["status"] == "succeeded"
    assert result["steps"]["b"]["skipped"] is True
    assert result["steps"]["b"]["benign_skip"] is True
    assert result["steps"]["c"]["skipped"] is True
    assert result["steps"]["c"]["benign_skip"] is True
    assert calls == ["A"]


def test_failed_dependency_is_not_benign(tmp_path):
    def fn(action, params, ctx):
        if params.get("id") == "A":
            return {"ok": False, "error": "boom"}
        return {"ok": True, "status": "completed"}

    execute = execute_factory([], fn)
    spec = {
        "workflow_id": "depfail",
        "fail_fast": False,
        "steps": [
            {"id": "a", "action": "work", "params": {"id": "A"}},
            {"id": "b", "action": "work", "depends_on": ["a"], "params": {"id": "B"}},
        ],
    }
    result = run_workflow(spec, execute=execute, context=ExecutionContext(), checkpoint_dir=tmp_path)
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["steps"]["a"]["ok"] is False
    assert "a" in result["failed_steps"]
    assert result["steps"]["b"]["skipped"] is True
    assert result["steps"]["b"].get("benign_skip") is not True


def test_verification_failure_fails_step(tmp_path):
    def fn(action, params, ctx):
        return {"ok": True, "stdout": "not what we need", "returncode": 0}

    execute = execute_factory([], fn)
    spec = {
        "workflow_id": "verify",
        "steps": [
            {
                "id": "a",
                "action": "work",
                "params": {},
                "verify": [{"type": "stdout_contains", "value": "expected"}],
            },
        ],
    }
    result = run_workflow(spec, execute=execute, context=ExecutionContext(), checkpoint_dir=tmp_path)
    assert result["ok"] is False
    assert result["steps"]["a"]["ok"] is False
    assert result["steps"]["a"]["verification"]["ok"] is False


def test_checkpoint_resume_reuses_succeeded_steps(tmp_path):
    checkpoint_dir = tmp_path / "checkpoints"
    calls: list[str] = []
    state = {"fail_b": True}

    def fn(action, params, ctx):
        calls.append(params["id"])
        if params["id"] == "B" and state["fail_b"]:
            return {"ok": False, "error": "temporary"}
        return {"ok": True, "id": params["id"], "status": "completed"}

    execute = execute_factory([], fn)
    spec = {
        "workflow_id": "resume",
        "fail_fast": False,
        "steps": [
            {"id": "a", "action": "work", "params": {"id": "A"}},
            {"id": "b", "action": "work", "depends_on": ["a"], "params": {"id": "B"}},
            {"id": "c", "action": "work", "depends_on": ["b"], "params": {"id": "C"}},
        ],
    }
    first = run_workflow(spec, execute=execute, context=ExecutionContext(), checkpoint_dir=checkpoint_dir)
    assert first["ok"] is False
    assert first["steps"]["a"]["ok"] is True
    assert "a" in first["succeeded_steps"]
    assert first["steps"]["b"]["ok"] is False
    assert (checkpoint_dir / "resume.json").exists()

    state["fail_b"] = False
    calls.clear()
    second = run_workflow(
        {**spec, "resume": True},
        execute=execute,
        context=ExecutionContext(),
        checkpoint_dir=checkpoint_dir,
    )
    assert second["ok"] is True, second.get("error")
    assert second["resumed"] is True
    assert "a" not in calls  # succeeded step is reused
    assert calls == ["B", "C"]
    assert second["steps"]["a"]["id"] == "A"


def test_checkpoint_spec_mismatch_is_rejected(tmp_path):
    checkpoint_dir = tmp_path / "checkpoints"
    spec = {
        "workflow_id": "mismatch",
        "steps": [{"id": "a", "action": "work", "params": {"x": 1}}],
    }

    def fn(action, params, ctx):
        return {"ok": True, "status": "completed"}

    execute = execute_factory([], fn)
    first = run_workflow(spec, execute=execute, context=ExecutionContext(), checkpoint_dir=checkpoint_dir)
    assert first["ok"] is True

    mismatch = run_workflow(
        {"workflow_id": "mismatch", "resume": True, "steps": [{"id": "a", "action": "work", "params": {"x": 2}}]},
        execute=execute,
        context=ExecutionContext(),
        checkpoint_dir=checkpoint_dir,
    )
    assert mismatch["ok"] is False
    assert mismatch["status"] == "checkpoint_mismatch"


def test_pre_cancelled_workflow_is_cancelled(tmp_path):
    def fn(action, params, ctx):
        return {"ok": True, "status": "completed"}

    execute = execute_factory([], fn)
    spec = {
        "workflow_id": "cancelled",
        "steps": [{"id": "a", "action": "work", "params": {}}],
    }
    ctx = ExecutionContext()
    ctx.cancellation.cancel("pre-cancel")
    result = run_workflow(spec, execute=execute, context=ctx, checkpoint_dir=tmp_path)
    assert result["ok"] is False
    assert result["status"] == "cancelled"
    assert result["cancelled"] is True
    assert result["steps"] == {}


def test_cancellation_during_step_marks_cancelled(tmp_path):
    token = ExecutionContext().cancellation
    started = threading.Event()

    def fn(action, params, ctx):
        started.set()
        if not ctx.cancellation.wait(10):
            return {"ok": True, "status": "completed"}
        return {"ok": False, "cancelled": True, "status": "cancelled"}

    execute = execute_factory([], fn)
    spec = {
        "workflow_id": "runtime-cancel",
        "steps": [{"id": "a", "action": "work", "params": {}}],
    }
    ctx = ExecutionContext(cancellation=token)

    def cancel_later():
        started.wait(timeout=5)
        time.sleep(0.2)
        token.cancel("runtime")

    threading.Thread(target=cancel_later, daemon=True).start()
    result = run_workflow(spec, execute=execute, context=ctx, checkpoint_dir=tmp_path)
    assert result["ok"] is False
    assert result["status"] == "cancelled"
    assert result["cancelled"] is True


def test_invalid_workflows_are_rejected(tmp_path):
    def fn(action, params, ctx):
        return {"ok": True}

    execute = execute_factory([], fn)
    invalid_specs = [
        {"steps": []},
        {"steps": [{"id": "a", "action": "work", "depends_on": ["missing"]}]},
        {"steps": [{"id": "a", "action": "work"}, {"id": "a", "action": "work"}]},
        {"steps": [{"id": "a", "action": "work", "depends_on": ["b"]}, {"id": "b", "action": "work", "depends_on": ["a"]}]},
    ]
    for spec in invalid_specs:
        result = run_workflow(spec, execute=execute, context=ExecutionContext(), checkpoint_dir=tmp_path)
        assert result["ok"] is False
        assert result["status"] == "invalid"

    # A step with no action
    result = run_workflow(
        {"steps": [{"id": "a"}]},
        execute=execute,
        context=ExecutionContext(),
        checkpoint_dir=tmp_path,
    )
    assert result["ok"] is False
    assert result["status"] == "invalid"


def test_verify_result_helper_covers_basic_types(tmp_path):
    target = tmp_path / "check.txt"
    target.write_text("hello world", encoding="utf-8")
    result = {"ok": True, "stdout": "hello", "stderr": "oops", "returncode": 0, "path": str(target), "status_code": 201}
    verification = verify_result(
        result,
        [
            {"type": "file_exists", "path": str(target)},
            {"type": "file_contains", "value": "hello"},
            {"type": "stdout_contains", "value": "hello"},
            {"type": "stderr_contains", "value": "oops"},
            {"type": "exit_code", "equals": 0},
            {"type": "http_status", "equals": [200, 201]},
            {"type": "ok", "truthy": True},
        ],
    )
    assert verification["ok"] is True
    assert all(row["ok"] for row in verification["checks"])

    bad = verify_result({"ok": False, "stdout": "x", "returncode": 1}, [{"type": "stdout_contains", "value": "y"}])
    assert bad["ok"] is False
    assert bad["checks"][0]["ok"] is False


def test_workflow_action_integration_through_executor(tmp_path, monkeypatch):
    import app.bridge.audit as audit_mod

    monkeypatch.setattr(audit_mod, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(audit_mod, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit_mod.audit, "path", tmp_path / "audit.jsonl")

    from app.bridge.context import ExecutionContext
    from app.bridge.executor import bridge_executor

    result = bridge_executor.execute(
        "workflow",
        {
            "workflow_id": "integration",
            "steps": [
                {"id": "a", "action": "echo", "params": {"x": 5}},
                {
                    "id": "b",
                    "action": "echo",
                    "depends_on": ["a"],
                    "params": {"x": "{{ steps.a.echo.x }}"},
                },
            ],
        },
        context=ExecutionContext(),
    )
    assert result["ok"] is True, result.get("error")
    assert result["steps"]["b"]["echo"]["x"] == 5
    assert result["checkpoint_path"].endswith("integration.json")
