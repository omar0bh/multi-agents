"""Durable workflow orchestration for the bridge.

Supports dependency graphs, parallel branches, retries, conditions, result
verification, cancellation and checkpoint/resume after a bridge restart.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from app.bridge.context import ExecutionContext
from app.bridge.runner import CancellationToken
from app.bridge.security import redact_obj

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHECKPOINT_DIR = Path(os.getenv("BRIDGE_WORKFLOW_DIR", str(PROJECT_ROOT / "data" / "bridge_workflows")))
MAX_PARALLEL = int(os.getenv("BRIDGE_WORKFLOW_MAX_PARALLEL", "8"))
MAX_STEPS = int(os.getenv("BRIDGE_WORKFLOW_MAX_STEPS", "200"))

_TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}|\$\{\s*([^{}]+?)\s*\}")
_FULL_TEMPLATE_RE = re.compile(r"^\s*(?:\{\{\s*([^{}]+?)\s*\}\}|\$\{\s*([^{}]+?)\s*\})\s*$")


class WorkflowError(Exception):
    pass


class TemplateError(WorkflowError):
    pass


def _sanitize_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return cleaned[:80] or f"wf_{uuid.uuid4().hex[:8]}"


def _lookup(path: str, root: dict[str, Any]) -> Any:
    parts = [p for p in path.strip().split(".") if p]
    current: Any = root
    for part in parts:
        if isinstance(current, dict):
            if part in current:
                current = current[part]
            elif part.isdigit() and int(part) in current:
                current = current[int(part)]
            else:
                raise TemplateError(f"Unknown template field: {path}")
        elif isinstance(current, (list, tuple)) and part.isdigit():
            try:
                current = current[int(part)]
            except IndexError as exc:
                raise TemplateError(f"Template index out of range: {path}") from exc
        else:
            raise TemplateError(f"Cannot resolve template field: {path}")
    return current


def resolve_templates(value: Any, state: dict[str, Any], *, max_depth: int = 20) -> Any:
    """Resolve ``{{ steps.id.stdout }}`` / ``${steps.id.path}`` references."""
    if max_depth <= 0:
        raise TemplateError("Template recursion limit exceeded")
    if isinstance(value, dict):
        return {k: resolve_templates(v, state, max_depth=max_depth - 1) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_templates(v, state, max_depth=max_depth - 1) for v in value]
    if not isinstance(value, str):
        return value

    full = _FULL_TEMPLATE_RE.match(value)
    if full:
        path = full.group(1) or full.group(2)
        return _lookup(path, state)

    def replace(match: re.Match[str]) -> str:
        path = match.group(1) or match.group(2)
        out = _lookup(path, state)
        if isinstance(out, (dict, list)):
            return json.dumps(out, ensure_ascii=False)
        return "" if out is None else str(out)

    return _TEMPLATE_RE.sub(replace, value)


def _evaluate_condition(condition: Any, state: dict[str, Any]) -> bool:
    if condition is None:
        return True
    if isinstance(condition, bool):
        return condition
    if isinstance(condition, str):
        text = condition.strip()
        if not text:
            return True
        if text.lower() in ("true", "yes", "1"):
            return True
        if text.lower() in ("false", "no", "0"):
            return False
        resolved = resolve_templates(condition, state)
        if isinstance(resolved, bool):
            return resolved
        return bool(resolved)
    if isinstance(condition, (int, float)):
        return bool(condition)
    if isinstance(condition, list):
        return all(_evaluate_condition(c, state) for c in condition)
    if not isinstance(condition, dict):
        return bool(condition)

    if "all" in condition:
        return all(_evaluate_condition(c, state) for c in condition["all"])
    if "any" in condition:
        return any(_evaluate_condition(c, state) for c in condition["any"])
    if "not" in condition:
        return not _evaluate_condition(condition["not"], state)

    if "step" in condition:
        step_id = str(condition["step"])
        step = state.get("steps", {}).get(step_id)
        if step is None:
            return False
        target: Any = step
        if condition.get("path"):
            target = _lookup(str(condition["path"]), step)
        if "equals" in condition:
            return target == resolve_templates(condition["equals"], state)
        if "not_equals" in condition:
            return target != resolve_templates(condition["not_equals"], state)
        if "contains" in condition:
            needle = resolve_templates(condition["contains"], state)
            try:
                return str(needle) in str(target)
            except Exception:
                return False
        if "exists" in condition:
            return (target is not None) == bool(condition["exists"])
        if "ok" in condition:
            return bool(step.get("ok")) == bool(condition["ok"])
        return bool(target)

    if "template" in condition:
        resolved = resolve_templates(str(condition["template"]), state)
        return bool(resolved)
    raise WorkflowError(f"Unsupported condition: {condition}")


def _read_text_for_check(path: str, max_chars: int = 2_000_000) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8", errors="replace")[:max_chars]


def verify_result(result: dict[str, Any], checks: Any, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify actual output/artifacts rather than trusting an action's ok flag."""
    if not checks:
        return {"ok": True, "checks": []}
    if isinstance(checks, dict):
        checks = [checks]
    if not isinstance(checks, list):
        return {"ok": False, "checks": [], "error": "verify must be a dict or list"}

    state = state or {"steps": {}}
    rows: list[dict[str, Any]] = []
    all_ok = True
    for raw in checks:
        if not isinstance(raw, dict):
            rows.append({"ok": False, "error": f"Invalid check: {raw!r}"})
            all_ok = False
            continue
        check = resolve_templates(raw, state)
        kind = check.get("type", "").lower()
        row: dict[str, Any] = {"type": kind, "ok": False}
        try:
            if kind in ("file_exists", "path_exists"):
                path = str(check.get("path") or result.get("path") or "")
                row["path"] = path
                row["ok"] = bool(path) and Path(path).expanduser().exists()
            elif kind in ("file_contains", "path_contains"):
                path = str(check.get("path") or result.get("path") or "")
                needle = str(check.get("value", check.get("text", "")))
                row.update({"path": path, "value": needle})
                row["ok"] = bool(path and needle) and needle in _read_text_for_check(path)
            elif kind in ("file_sha256", "sha256"):
                import hashlib as _hashlib

                path = str(check.get("path") or result.get("path") or "")
                expected = str(check.get("equals", check.get("value", ""))).lower()
                digest = _hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()
                row.update({"path": path, "sha256": digest, "expected": expected})
                row["ok"] = bool(expected) and digest == expected
            elif kind in ("stdout_contains", "stderr_contains", "output_contains"):
                field_name = "stderr" if kind == "stderr_contains" else "stdout"
                text = str(result.get(field_name, ""))
                needle = str(check.get("value", check.get("text", "")))
                row.update({"field": field_name, "value": needle})
                row["ok"] = needle in text
            elif kind in ("exit_code", "returncode"):
                expected = check.get("equals", check.get("value", 0))
                actual = result.get("returncode", result.get("exit_code"))
                row.update({"actual": actual, "expected": expected})
                row["ok"] = actual == expected
            elif kind in ("json_path", "json_equals"):
                import json as _json

                payload = result.get("json")
                if payload is None and isinstance(result.get("body"), str):
                    try:
                        payload = _json.loads(result["body"])
                    except Exception:
                        payload = None
                target = _lookup(str(check.get("path", "")), payload or {}) if check.get("path") else payload
                row["actual"] = target
                if "equals" in check:
                    row["expected"] = check["equals"]
                    row["ok"] = target == check["equals"]
                else:
                    row["ok"] = target is not None
            elif kind in ("http_status", "status_code"):
                expected = check.get("equals", check.get("value", 200))
                actual = result.get("status_code")
                row.update({"actual": actual, "expected": expected})
                if isinstance(expected, list):
                    row["ok"] = actual in expected
                else:
                    row["ok"] = actual == expected
            elif kind in ("ok", "truthy"):
                row["ok"] = bool(result.get("ok"))
            elif kind in ("not_ok", "falsy"):
                row["ok"] = not bool(result.get("ok"))
            else:
                row["error"] = f"Unknown verification type: {kind}"
        except Exception as exc:
            row["error"] = str(exc)
            row["ok"] = False
        all_ok = all_ok and bool(row.get("ok"))
        rows.append(row)
    return {"ok": all_ok, "checks": rows}


def _checkpoint_path(workflow_id: str) -> Path:
    return CHECKPOINT_DIR / f"{workflow_id}.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(redact_obj(payload), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


#: Control fields that do not change what a workflow does.  They are excluded
#: from the checkpoint hash so a run can be resumed by adding ``resume: true``
#: without invalidating the checkpoint from the previous attempt.
_NON_EXECUTION_SPEC_KEYS = {"resume", "checkpoint_dir"}


def _spec_hash(spec: dict[str, Any]) -> str:
    stable_spec = {key: value for key, value in spec.items() if key not in _NON_EXECUTION_SPEC_KEYS}
    stable = json.dumps(stable_spec, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def validate_workflow(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("Workflow requires a non-empty 'steps' list")
    if len(steps) > MAX_STEPS:
        raise WorkflowError(f"Workflow has {len(steps)} steps; maximum is {MAX_STEPS}")
    by_id: dict[str, dict[str, Any]] = {}
    for idx, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise WorkflowError(f"Step {idx} must be an object")
        step_id = str(raw.get("id") or raw.get("name") or f"step_{idx + 1}")
        if step_id in by_id:
            raise WorkflowError(f"Duplicate step id: {step_id}")
        if not raw.get("action"):
            raise WorkflowError(f"Step '{step_id}' is missing 'action'")
        step = dict(raw)
        step["id"] = step_id
        deps = step.get("depends_on", step.get("deps", []))
        if isinstance(deps, str):
            deps = [deps]
        step["depends_on"] = [str(d) for d in (deps or [])]
        by_id[step_id] = step

    for step in by_id.values():
        for dep in step["depends_on"]:
            if dep not in by_id:
                raise WorkflowError(f"Step '{step['id']}' depends on unknown step '{dep}'")
            if dep == step["id"]:
                raise WorkflowError(f"Step '{step['id']}' cannot depend on itself")

    # Cycle detection (DFS).
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, chain: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            raise WorkflowError(f"Dependency cycle: {' -> '.join(chain + [node])}")
        visiting.add(node)
        for dep in by_id[node]["depends_on"]:
            visit(dep, chain + [node])
        visiting.remove(node)
        visited.add(node)

    for step_id in by_id:
        visit(step_id, [])

    ordered = list(steps)
    for step in ordered:
        if "id" not in step or not step.get("id"):
            step["id"] = next(sid for sid, candidate in by_id.items() if candidate is not step)
    return ordered, by_id


def run_workflow(
    spec: dict[str, Any],
    *,
    execute: Callable[[str, dict[str, Any], ExecutionContext], dict[str, Any]],
    context: ExecutionContext | None = None,
    checkpoint_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute a workflow spec and return a structured report."""
    started = time.time()
    context = context or ExecutionContext()
    checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else CHECKPOINT_DIR
    workflow_id = _sanitize_id(str(spec.get("workflow_id") or spec.get("id") or context.task_id or f"wf_{uuid.uuid4().hex[:8]}"))
    max_parallel = max(1, min(int(spec.get("max_parallel", 4) or 4), MAX_PARALLEL))
    fail_fast = bool(spec.get("fail_fast", True))
    resume = bool(spec.get("resume", False))
    ckpt_path = checkpoint_dir / f"{workflow_id}.json"
    spec_hash = _spec_hash(spec)

    try:
        _, by_id = validate_workflow(spec)
    except WorkflowError as exc:
        return {
            "ok": False,
            "action": "workflow",
            "workflow_id": workflow_id,
            "status": "invalid",
            "error": str(exc),
            "started_at": started,
            "finished_at": time.time(),
            "duration_ms": int((time.time() - started) * 1000),
        }

    state: dict[str, Any] = {
        "workflow_id": workflow_id,
        "steps": {},
        "started_at": started,
    }
    statuses: dict[str, str] = {sid: "pending" for sid in by_id}
    resumed = False

    if resume and ckpt_path.exists():
        try:
            checkpoint = json.loads(ckpt_path.read_text(encoding="utf-8"))
            if checkpoint.get("spec_hash") not in (None, spec_hash):
                return {
                    "ok": False,
                    "action": "workflow",
                    "workflow_id": workflow_id,
                    "status": "checkpoint_mismatch",
                    "error": "Checkpoint was created from a different workflow spec; pass resume=false or a new workflow_id.",
                    "checkpoint_path": str(ckpt_path),
                }
            for step_id, row in (checkpoint.get("steps") or {}).items():
                if step_id in by_id and row.get("status") == "succeeded":
                    # Only completed work is reused.  Failed/skipped steps are
                    # retried on resume so a transient failure can recover.
                    statuses[step_id] = "succeeded"
                    state["steps"][step_id] = row.get("result") or {}
                    resumed = True
        except Exception as exc:
            logger.warning("Could not read checkpoint %s: %s", ckpt_path, exc)

    def save_checkpoint(status: str, error: str = "") -> None:
        payload = {
            "workflow_id": workflow_id,
            "spec_hash": spec_hash,
            "status": status,
            "updated_at": time.time(),
            "started_at": started,
            "error": error,
            "steps": {
                sid: {
                    "status": statuses[sid],
                    "result": state["steps"].get(sid),
                }
                for sid in by_id
            },
        }
        try:
            _atomic_write(checkpoint_dir / f"{workflow_id}.json", payload)
        except Exception as exc:
            logger.warning("Checkpoint write failed: %s", exc)

    results: dict[str, dict[str, Any]] = {}
    attempts_used: dict[str, int] = {}
    total = len(by_id)
    finished_count = sum(1 for s in statuses.values() if s in ("succeeded", "failed", "skipped"))
    context.progress(percent=round(finished_count / total * 100, 1), message=f"Workflow {workflow_id} starting", force=True)

    def step_ready(step_id: str) -> bool:
        deps = by_id[step_id]["depends_on"]
        return all(statuses[d] == "succeeded" for d in deps)

    def deps_failed(step_id: str) -> bool:
        return any(statuses[d] in ("failed", "skipped", "cancelled") for d in by_id[step_id]["depends_on"])

    def deps_skipped_benignly(step_id: str) -> bool:
        deps = by_id[step_id]["depends_on"]
        if not deps:
            return False
        for dep in deps:
            status = statuses[dep]
            if status in ("failed", "cancelled"):
                return False
            if status == "skipped" and not state["steps"].get(dep, {}).get("benign_skip"):
                return False
        return True

    def run_step(step_id: str) -> dict[str, Any]:
        step = by_id[step_id]
        child = context.child(step_id)
        max_attempts = max(1, min(int(step.get("retries", 0) or 0) + 1, 5))
        retry_delay = max(0.0, float(step.get("retry_delay", 1.0) or 0))
        last: dict[str, Any] = {}
        for attempt in range(1, max_attempts + 1):
            attempts_used[step_id] = attempt
            child.check()
            child.progress(message=f"Step {step_id} ({step.get('action')}) attempt {attempt}", step_id=step_id)
            params = resolve_templates(step.get("params", {}), state)
            if not isinstance(params, dict):
                return {"ok": False, "action": step.get("action"), "error": "Step params must resolve to an object"}
            if step.get("timeout") and "timeout" not in params:
                params["timeout"] = step["timeout"]
            try:
                result = execute(str(step["action"]), params, child)
            except Exception as exc:
                result = {"ok": False, "action": step.get("action"), "error": f"{type(exc).__name__}: {exc}"}
            if not isinstance(result, dict):
                result = {"ok": False, "action": step.get("action"), "error": "Action returned a non-object result"}
            result.setdefault("ok", False)
            result.setdefault("action", step.get("action"))
            result["attempt"] = attempt

            checks = step.get("verify", step.get("expect"))
            if checks:
                verification = verify_result(result, checks, state)
                result["verification"] = verification
                if verification.get("ok") and result.get("ok") and not result.get("verified"):
                    result["verified"] = True
                if not verification.get("ok"):
                    result["ok"] = False
                    if not result.get("error"):
                        result["error"] = "Result verification failed"
            elif result.get("ok"):
                # Honest verification: a successful command/file action carries
                # its own exit code/hash evidence; workflows without explicit
                # checks still mark verified only if evidence is present.
                result["verified"] = bool(
                    result.get("verified")
                    or "returncode" in result
                    or "sha256" in result
                    or result.get("status") == "completed"
                )

            last = result
            if result.get("ok") or attempt >= max_attempts or context.cancelled:
                break
            child.progress(message=f"Retrying step {step_id} after failure ({attempt}/{max_attempts})", step_id=step_id, force=True)
            if child.cancellation.wait(retry_delay):
                break
        return last

    executor = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="bridge-wf")
    futures: dict[Any, str] = {}
    try:
        while True:
            if context.cancelled:
                for sid, status in statuses.items():
                    if status == "pending":
                        statuses[sid] = "cancelled"
                break

            # Mark un-runnable dependents as skipped.
            for sid, status in list(statuses.items()):
                if status == "pending" and deps_failed(sid):
                    benign = deps_skipped_benignly(sid)
                    statuses[sid] = "skipped"
                    finished_count += 1
                    state["steps"][sid] = {
                        "ok": benign,
                        "skipped": True,
                        "benign_skip": benign,
                        "action": by_id[sid].get("action"),
                        "reason": "Dependency skipped by condition" if benign else "Dependency failed",
                        **({} if benign else {"error": "Dependency failed"}),
                    }
                    results[sid] = state["steps"][sid]

            # Schedule ready steps.
            condition_skipped = False
            ready = [
                sid
                for sid, status in statuses.items()
                if status == "pending" and step_ready(sid) and sid not in futures.values()
            ]
            for sid in ready:
                if len(futures) >= max_parallel:
                    break
                if not _evaluate_condition(by_id[sid].get("when"), state):
                    statuses[sid] = "skipped"
                    finished_count += 1
                    state["steps"][sid] = {
                        "ok": True,
                        "skipped": True,
                        "benign_skip": True,
                        "action": by_id[sid].get("action"),
                        "reason": "when condition false",
                    }
                    results[sid] = state["steps"][sid]
                    condition_skipped = True
                    save_checkpoint("running")
                    continue
                fut = executor.submit(run_step, sid)
                futures[fut] = sid

            if not futures:
                pending = [sid for sid, s in statuses.items() if s in ("pending",)]
                if not pending:
                    break
                if condition_skipped:
                    # Re-evaluate: dependents of the skipped step may now be
                    # skippable via deps_failed() at the top of the next pass.
                    continue
                # Should be impossible after cycle validation, but fail loud.
                return {
                    "ok": False,
                    "action": "workflow",
                    "workflow_id": workflow_id,
                    "status": "deadlock",
                    "error": f"No runnable steps; pending: {pending}",
                    "steps": state["steps"],
                }

            done, _ = wait(list(futures), return_when=FIRST_COMPLETED, timeout=1.0)
            for fut in done:
                sid = futures.pop(fut)
                try:
                    result = fut.result()
                except Exception as exc:
                    result = {"ok": False, "action": by_id[sid].get("action"), "error": f"{type(exc).__name__}: {exc}"}
                results[sid] = result
                state["steps"][sid] = result
                statuses[sid] = "succeeded" if result.get("ok") else "failed"
                finished_count += 1
                context.progress(
                    percent=round(finished_count / total * 100, 1),
                    message=f"Step {sid} {statuses[sid]}",
                    step_id=sid,
                    force=True,
                )
                save_checkpoint("running")

                if not result.get("ok") and fail_fast and not by_id[sid].get("continue_on_error"):
                    for other, st in statuses.items():
                        if st == "pending":
                            statuses[other] = "skipped"
                            state["steps"][other] = {"ok": False, "skipped": True, "action": by_id[other].get("action"), "error": f"Skipped after {sid} failed (fail_fast)"}
                    break

            if fail_fast and any(s == "failed" for s in statuses.values()):
                for other, st in statuses.items():
                    if st == "pending":
                        statuses[other] = "skipped"
                        state["steps"][other] = {"ok": False, "skipped": True, "action": by_id[other].get("action"), "error": "Skipped after failure (fail_fast)"}
                # Let currently running steps finish; they share the same token
                # only when the caller cancels.  Break scheduling but drain.
                if not futures:
                    break
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    succeeded = [sid for sid, s in statuses.items() if s == "succeeded"]
    failed = [sid for sid, s in statuses.items() if s == "failed"]
    skipped = [sid for sid, s in statuses.items() if s == "skipped"]
    cancelled = context.cancelled or any(s == "cancelled" for s in statuses.values())
    # A step skipped because its own `when` condition was false is expected;
    # any other skip (dependency failure, fail_fast, cancellation) is not a
    # successful workflow.
    benign_skips = {
        sid
        for sid, status in statuses.items()
        if status == "skipped"
        and (
            state["steps"].get(sid, {}).get("benign_skip")
            or state["steps"].get(sid, {}).get("reason") == "when condition false"
        )
    }
    ok = (
        bool(statuses)
        and not failed
        and not cancelled
        and all(status == "succeeded" or (status == "skipped" and sid in benign_skips) for sid, status in statuses.items())
    )
    status = "cancelled" if cancelled else ("succeeded" if ok else "failed")
    save_checkpoint(status, error="; ".join(str(results[s].get("error", "")) for s in failed)[:2000])

    finished = time.time()
    report_lines = [
        f"Workflow `{workflow_id}`: {status}",
        f"Steps: {len(succeeded)} succeeded, {len(failed)} failed, {len(skipped)} skipped",
    ]
    for sid in by_id:
        row = state["steps"].get(sid, {})
        report_lines.append(f"  - {sid}: {statuses[sid]} ({row.get('action', '?')})")
        if statuses[sid] == "failed" and row.get("error"):
            report_lines.append(f"      error: {row['error']}")

    return {
        "ok": ok,
        "action": "workflow",
        "workflow_id": workflow_id,
        "status": status,
        "started_at": started,
        "finished_at": finished,
        "duration_ms": int((finished - started) * 1000),
        "total_steps": total,
        "succeeded_steps": succeeded,
        "failed_steps": failed,
        "skipped_steps": skipped,
        "cancelled": cancelled,
        "resumed": resumed,
        "checkpoint_path": str(ckpt_path),
        "steps": redact_obj(state["steps"]),
        "report": "\n".join(report_lines),
        "detail": "\n".join(report_lines),
        "artifacts": _collect_artifacts(state["steps"]),
    }


def _collect_artifacts(steps: dict[str, Any]) -> list[str]:
    artifacts: list[str] = []
    for result in steps.values():
        if not isinstance(result, dict):
            continue
        for key in ("path", "artifact", "screenshot_path", "txt_path", "csv_path"):
            value = result.get(key)
            if isinstance(value, str) and value and value not in artifacts:
                artifacts.append(value)
        for value in result.get("artifacts", []) or []:
            if isinstance(value, str) and value not in artifacts:
                artifacts.append(value)
    return artifacts[:200]
