"""Built-in bridge actions and the action registry.

Every action returns a JSON-serialisable dictionary.  The executor adds
execution metadata, cancellation, audit and history around these handlers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.bridge import cloud as cloud_mod
from app.bridge.audit import audit, history_stats, list_executions, record_execution
from app.bridge.context import ExecutionContext
from app.bridge.runner import (
    DEFAULT_MAX_OUTPUT,
    ManagedProcess,
    clamp_timeout,
    interpreter_args,
    pid_alive,
    process_manager,
    resolve_interpreter,
    resources,
    run_process,
    truncate_output,
)
from app.bridge.security import (
    approval_valid,
    check_command,
    check_path,
    redact,
    redact_obj,
    sanitized_env,
    security_summary,
)
from app.bridge.workflow import run_workflow, verify_result, WorkflowError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
Handler = Callable[[dict[str, Any], ExecutionContext, Any], dict[str, Any]]


@dataclass
class ActionSpec:
    name: str
    handler: Handler
    category: str = "general"
    description: str = ""
    risk: str = "low"
    aliases: tuple[str, ...] = ()
    timeout: int | None = None


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, ActionSpec] = {}
        self._aliases: dict[str, str] = {}

    def register(
        self,
        name: str,
        *,
        category: str = "general",
        description: str = "",
        risk: str = "low",
        aliases: tuple[str, ...] = (),
        timeout: int | None = None,
    ) -> Callable[[Handler], Handler]:
        def decorator(fn: Handler) -> Handler:
            spec = ActionSpec(name, fn, category, description, risk, aliases, timeout)
            self._actions[name] = spec
            for alias in aliases:
                self._aliases[alias] = name
            return fn

        return decorator

    def resolve(self, action: str) -> ActionSpec | None:
        key = (action or "").strip().lower()
        key = self._aliases.get(key, key)
        return self._actions.get(key)

    def all(self) -> list[ActionSpec]:
        return sorted(self._actions.values(), key=lambda s: s.name)

    def manifest(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "aliases": list(spec.aliases),
                "category": spec.category,
                "description": spec.description,
                "risk": spec.risk,
                "timeout": spec.timeout,
            }
            for spec in self.all()
        ]


actions = ActionRegistry()


def register(*args: Any, **kwargs: Any) -> Callable[[Handler], Handler]:
    return actions.register(*args, **kwargs)


def ok(action: str, **fields: Any) -> dict[str, Any]:
    return {"ok": True, "action": action, "status": "completed", **fields}


def err(action: str, error: str, **fields: Any) -> dict[str, Any]:
    return {"ok": False, "action": action, "status": "failed", "error": redact(error), **fields}


def blocked(action: str, decision: Any) -> dict[str, Any]:
    data = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision)
    return {
        "ok": False,
        "action": action,
        "status": "blocked",
        "blocked": True,
        "error": data.get("reason", "Blocked by security policy"),
        "security": data,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _known() -> dict[str, Path]:
    from app.tools.user_folders import get_known_user_folders

    return get_known_user_folders()


def _desktop() -> Path:
    return _known().get("desktop", Path.home() / "Desktop")


def _strip(value: Any) -> str:
    return str(value or "").strip().strip("'\"`")


def _resolve_folder(folder: str = "", parent: str = "", default: str = "desktop", create: bool = False) -> Path | None:
    known = _known()
    folder = _strip(folder)
    parent = _strip(parent).lower()
    if folder:
        direct = Path(os.path.expandvars(folder)).expanduser()
        if direct.is_dir():
            return direct.resolve()
        if folder.lower() in known:
            return known[folder.lower()]
        if parent and parent in known:
            candidate = known[parent] / folder
            if candidate.is_dir() or create:
                return candidate.resolve()
        for base in (
            known.get("desktop"),
            known.get("documents"),
            known.get("downloads"),
            known.get("pictures"),
            known.get("videos"),
            known.get("music"),
            Path.home(),
        ):
            if not base:
                continue
            candidate = base / folder
            if candidate.is_dir():
                return candidate.resolve()
        if create:
            return (_desktop() / folder).resolve()
        return None
    if parent:
        if parent in known:
            return known[parent]
        candidate = _desktop() / parent
        if candidate.is_dir() or create:
            return candidate.resolve()
        direct = Path(os.path.expandvars(parent)).expanduser()
        if direct.is_dir():
            return direct.resolve()
    return known.get(default, _desktop())


def _resolve_read_target(query: str) -> Path | None:
    cleaned = _strip(query)
    if not cleaned:
        return None
    direct = Path(os.path.expandvars(cleaned)).expanduser()
    if direct.is_file():
        return direct.resolve()
    in_project = (PROJECT_ROOT / cleaned).resolve()
    if in_project.is_file():
        return in_project
    known = _known()
    for base in known.values():
        candidate = (base / cleaned).resolve()
        if candidate.is_file():
            return candidate
    target_name = Path(cleaned).name.lower()
    desktop = known.get("desktop", Path.home() / "Desktop")
    for root, dirs, files in os.walk(desktop):
        rel = Path(root).relative_to(desktop)
        if len(rel.parts) > 3:
            dirs.clear()
            continue
        for f in files:
            if f.lower() == target_name:
                return (Path(root) / f).resolve()
    try:
        from app.tools.file_reader import find_file

        found = find_file(cleaned)
        return found if found and found.is_file() else None
    except Exception:
        return None


def _resolve_existing_source(src: str, src_parent: str = "") -> Path | None:
    src = _strip(src)
    if not src:
        return None
    known = _known()
    desktop = known.get("desktop", Path.home() / "Desktop")
    if src_parent:
        parent = _resolve_folder(src_parent)
        if parent and (parent / src).exists():
            return (parent / src).resolve()
    direct = Path(os.path.expandvars(src)).expanduser()
    if direct.exists():
        return direct.resolve()
    if (desktop / src).exists():
        return (desktop / src).resolve()
    for base in known.values():
        if (base / src).exists():
            return (base / src).resolve()
    target_name = Path(src).name.lower()
    for root, dirs, files in os.walk(desktop):
        rel = Path(root).relative_to(desktop)
        if len(rel.parts) > 3:
            dirs.clear()
            continue
        for name in dirs + files:
            if name.lower() == target_name:
                return (Path(root) / name).resolve()
    try:
        from app.tools.file_reader import find_file

        return find_file(src)
    except Exception:
        return None


def _approval(params: dict[str, Any]) -> str | None:
    return params.get("approval_token") or params.get("_approval_token")


def _policy_error(action: str, decision: Any) -> dict[str, Any]:
    return blocked(action, decision)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_atomic(path: Path, content: str | bytes, *, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path.with_name(f"{path.name}.{stamp}.bak").write_bytes(path.read_bytes())
    data = content.encode("utf-8") if isinstance(content, str) else content
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _entries(path: Path, *, hidden: bool = False, limit: int = 200) -> tuple[list[str], list[dict[str, Any]]]:
    items: list[str] = []
    rows: list[dict[str, Any]] = []
    try:
        iterator = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError as exc:
        raise PermissionError(f"Cannot list {path}: {exc}") from exc
    for entry in iterator:
        if not hidden and entry.name.startswith((".", "__")):
            continue
        is_dir = entry.is_dir()
        try:
            stat = entry.stat()
            size = None if is_dir else stat.st_size
            mtime = stat.st_mtime
        except OSError:
            size = None
            mtime = None
        items.append(f"{'📁' if is_dir else '📄'} {entry.name}")
        rows.append(
            {
                "name": entry.name,
                "path": str(entry),
                "type": "directory" if is_dir else "file",
                "size_bytes": size,
                "modified_at": mtime,
            }
        )
        if len(rows) >= max(1, int(limit)):
            break
    return items, rows


def _folder_for_write(params: dict[str, Any], default: str = "desktop") -> tuple[Path | None, str]:
    folder = _strip(params.get("folder") or params.get("dest_folder") or params.get("parent"))
    parent = _strip(params.get("parent") if params.get("folder") else "")
    path = _resolve_folder(folder, parent=parent, default=default, create=True)
    return path, folder


# ---------------------------------------------------------------------------
# Diagnostics / discovery
# ---------------------------------------------------------------------------


@register("echo", category="diagnostics", description="Echo params back; useful for connectivity tests")
def action_echo(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    return ok("echo", echo=params, detail="Bridge echo successful.")


@register("system_info", category="diagnostics", description="Host, OS, paths, capabilities and security configuration")
def action_system_info(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    import platform

    from app.bridge.capabilities import bridge_capabilities

    info = {
        "hostname": os.environ.get("COMPUTERNAME") or platform.node(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER"),
        "cwd": os.getcwd(),
        "project_root": str(PROJECT_ROOT),
        "user_home": str(Path.home()),
        "known_folders": {k: str(v) for k, v in _known().items()},
        "resources": resources.snapshot(),
        "security": security_summary(),
        "cloud_providers": cloud_mod.cloud_providers_info(check_availability=False),
        "capabilities": bridge_capabilities(include_actions=not bool(params.get("compact"))),
    }
    return ok("system_info", **info, detail=f"System info for {info['hostname']} ({info['os']}).")


@register("resource_snapshot", category="diagnostics", description="CPU, memory, disk and GPU capacity")
def action_resource_snapshot(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    snap = resources.snapshot(cwd=params.get("cwd"), force=bool(params.get("force")))
    return ok("resource_snapshot", resources=snap, capacity=resources.capacity(cwd=params.get("cwd")), detail="Resource snapshot captured.")


@register("security_check", category="security", description="Dry-run policy check for a command or path")
def action_security_check(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    if params.get("command") or params.get("cmd"):
        decision = check_command(str(params.get("command") or params.get("cmd")), cwd=params.get("cwd"), approval_token=_approval(params))
    elif params.get("path"):
        decision = check_path(params["path"], str(params.get("operation", "write")), approval_token=_approval(params))
    else:
        return err("security_check", "Provide 'command' or 'path'")
    data = decision.as_dict()
    return {"ok": True, "action": "security_check", "allowed": decision.allowed, "decision": data, "detail": decision.reason}


@register("history", category="diagnostics", description="Recent local execution history")
def action_history(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    rows = list_executions(limit=int(params.get("limit", 20)), action=params.get("filter_action"))
    return ok("history", executions=rows, stats=history_stats(), count=len(rows), detail=f"{len(rows)} execution(s).")


@register("audit_tail", category="security", description="Tail the structured audit log")
def action_audit_tail(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    events = audit.tail(int(params.get("limit", 50)))
    return ok("audit_tail", events=events, count=len(events), detail=f"{len(events)} audit event(s).")


# ---------------------------------------------------------------------------
# Shell / code
# ---------------------------------------------------------------------------


@register("run_command", category="shell", description="Run a local shell command with timeout, cancellation and policy checks", risk="variable", aliases=("shell", "exec", "terminal", "cmd"))
def action_run_command(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    command = params.get("cmd") or params.get("command") or params.get("script")
    if isinstance(command, list):
        command = [str(c) for c in command]
    if not command:
        return err("run_command", "No command supplied (use 'cmd' or 'command').")
    cwd = params.get("cwd") or params.get("workdir") or str(PROJECT_ROOT)
    timeout = clamp_timeout(params.get("timeout", params.get("timeout_seconds", 120)))
    shell = bool(params.get("shell", not isinstance(command, list)))
    decision = check_command(command, cwd=cwd, approval_token=_approval(params), sandbox=bool(params.get("sandbox")))
    if not decision.allowed:
        return blocked("run_command", decision)

    retries = max(0, min(int(params.get("retries", 0) or 0), 3))
    if decision.risk == "high" and retries:
        retries = 0
    last: dict[str, Any] | None = None
    for attempt in range(retries + 1):
        ctx.check()
        if attempt:
            ctx.progress(message=f"Retrying command ({attempt}/{retries})", force=True)
            if ctx.cancellation.wait(min(2.0 * attempt, 10.0)):
                break
        result = run_process(
            command,
            cwd=cwd,
            timeout=timeout,
            shell=shell,
            env=params.get("env") if isinstance(params.get("env"), dict) else None,
            input_text=params.get("stdin"),
            max_output=int(params.get("max_output", DEFAULT_MAX_OUTPUT)),
            token=ctx.cancellation,
        )
        data = result.as_dict()
        data.update({"action": "run_command", "risk": decision.risk, "security": decision.as_dict(), "attempt": attempt + 1})
        data["status"] = "completed" if result.ok else ("cancelled" if result.cancelled else ("timeout" if result.timed_out else "failed"))
        data["verified"] = result.ok and not result.timed_out and not result.cancelled
        if result.stdout:
            data["detail"] = result.stdout
        last = data
        if result.ok or result.cancelled or result.timed_out:
            break
    return last or err("run_command", "Command did not run")


@register("run_code", category="code", description="Execute code in Python, Node, PowerShell, Bash and more with a timeout", risk="medium", aliases=("execute_code", "python", "node"))
def action_run_code(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    code = params.get("code")
    if code is None:
        return err("run_code", "No 'code' supplied")
    language = str(params.get("language") or params.get("lang") or "python")
    code_text = str(code)
    # Inspect the script itself before writing or executing it.  This blocks
    # critical commands and protected Windows paths reached through code
    # (rmtree/unlink/write_text/open-for-write), not just shell commands.
    precheck = check_command(code_text, cwd=params.get("cwd"), approval_token=_approval(params), sandbox=True)
    if not precheck.allowed:
        return blocked("run_code", precheck)
    exe, ext, lookup_error = resolve_interpreter(language)
    if not exe:
        return err("run_code", lookup_error)
    timeout = clamp_timeout(params.get("timeout", 120))
    keep = bool(params.get("keep_workspace", False))
    cwd = params.get("cwd") or params.get("workdir")
    workdir: Path
    temp_created = False
    if cwd:
        workdir = Path(os.path.expandvars(str(cwd))).expanduser().resolve()
        decision = check_path(workdir, "write", approval_token=_approval(params))
        if not decision.allowed:
            return blocked("run_code", decision)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        import tempfile

        workdir = Path(tempfile.mkdtemp(prefix="bridge_code_"))
        temp_created = True

    files = params.get("files") or {}
    if not isinstance(files, dict):
        return err("run_code", "'files' must be an object of relative_path -> content")
    try:
        for rel, content in files.items():
            rel_path = Path(str(rel))
            if rel_path.is_absolute() or ".." in rel_path.parts:
                return err("run_code", f"Unsafe relative file path: {rel}")
            target = workdir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
        filename = str(params.get("filename") or f"bridge_job{ext}")
        raw_script = Path(os.path.expandvars(filename)).expanduser()
        script = (workdir / raw_script).resolve() if not raw_script.is_absolute() else raw_script.resolve()
        try:
            script.relative_to(workdir.resolve())
            inside_workspace = True
        except ValueError:
            inside_workspace = False
        if not inside_workspace:
            script_decision = check_path(script, "write", approval_token=_approval(params))
            if not script_decision.allowed:
                return blocked("run_code", script_decision)
        script.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(script, code_text)
        args = params.get("args") or []
        if isinstance(args, str):
            args = [args]
        argv = interpreter_args(language, exe, script, args)
        decision = check_command(argv, cwd=workdir, approval_token=_approval(params), sandbox=True)
        # `run_code` is expected to execute code; the command policy still
        # blocks critical strings inside the code itself.
        if not decision.allowed:
            return blocked("run_code", decision)
        ctx.progress(message=f"Executing {language} code", percent=10, force=True)
        result = run_process(
            argv,
            cwd=workdir,
            timeout=timeout,
            shell=False,
            env=params.get("env") if isinstance(params.get("env"), dict) else None,
            input_text=params.get("stdin"),
            max_output=int(params.get("max_output", DEFAULT_MAX_OUTPUT)),
            token=ctx.cancellation,
        )
        data = result.as_dict()
        data.update(
            {
                "action": "run_code",
                "language": language,
                "interpreter": exe,
                "script": str(script),
                "workspace": str(workdir),
                "kept_workspace": keep or bool(cwd),
                "risk": decision.risk,
                "status": "completed" if result.ok else ("cancelled" if result.cancelled else ("timeout" if result.timed_out else "failed")),
                "verified": result.ok and not result.timed_out and not result.cancelled,
            }
        )
        if result.ok:
            data["detail"] = result.stdout or "Code executed successfully (no output)."
        return data
    finally:
        if temp_created and not keep:
            shutil.rmtree(workdir, ignore_errors=True)


@register("package_install", category="environment", description="Install dependencies through pip/npm/cargo/go/winget with policy checks", risk="medium")
def action_package_install(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    manager = str(params.get("manager") or params.get("tool") or "pip").lower()
    packages = params.get("packages") or params.get("package")
    if isinstance(packages, str):
        packages = [packages]
    if not isinstance(packages, list) or not packages:
        return err("package_install", "Provide a list of packages")
    for pkg in packages:
        if not re.match(r"^[A-Za-z0-9_.:/@+\-\[\]>=<~^,]+$", str(pkg)):
            return err("package_install", f"Unsafe package specifier: {pkg}")
    commands = {
        "pip": ["python", "-m", "pip", "install", *map(str, packages)],
        "pip3": ["python3", "-m", "pip", "install", *map(str, packages)],
        "npm": ["npm", "install", *map(str, packages)],
        "pnpm": ["pnpm", "add", *map(str, packages)],
        "yarn": ["yarn", "add", *map(str, packages)],
        "cargo": ["cargo", "install", *map(str, packages)],
        "go": ["go", "install", *map(str, packages)],
        "winget": ["winget", "install", "--accept-package-agreements", "--accept-source-agreements", *map(str, packages)],
        "choco": ["choco", "install", "-y", *map(str, packages)],
        "apt": ["apt-get", "install", "-y", *map(str, packages)],
    }
    if manager not in commands:
        return err("package_install", f"Unsupported package manager: {manager}")
    argv = commands[manager]
    decision = check_command(argv, cwd=params.get("cwd"), approval_token=_approval(params))
    if not decision.allowed:
        return blocked("package_install", decision)
    result = run_process(argv, cwd=params.get("cwd") or PROJECT_ROOT, timeout=clamp_timeout(params.get("timeout", 900)), shell=False, token=ctx.cancellation)
    data = result.as_dict()
    data.update({"action": "package_install", "manager": manager, "packages": packages, "security": decision.as_dict()})
    data["status"] = "completed" if result.ok else "failed"
    return data


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@register("list_dir", category="files", description="List a directory with structured entries", aliases=("ls", "dir", "list_directory"))
def action_list_dir(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    folder = _strip(params.get("folder") or params.get("path") or params.get("src"))
    parent = _strip(params.get("parent") or params.get("dest_folder"))
    if not folder and parent:
        target = _resolve_folder("", parent=parent)
    elif folder:
        target = _resolve_folder(folder, parent=parent)
        if target is None:
            direct = Path(os.path.expandvars(folder)).expanduser()
            target = direct if direct.is_dir() else None
    else:
        target = _resolve_folder("", default="desktop")
    if not target or not target.is_dir():
        return err("list_dir", f"Folder not found: {folder or parent or 'desktop'}", desktop=str(_desktop()))
    decision = check_path(target, "read")
    if not decision.allowed:
        return blocked("list_dir", decision)
    items, rows = _entries(target, hidden=bool(params.get("hidden")), limit=int(params.get("limit", 200)))
    return ok(
        "list_dir",
        path=str(target),
        folder_name=target.name,
        items=items,
        entries=rows,
        count=len(rows),
        detail="\n".join(items) if items else "المجلد فارغ.",
    )


@register("read_file", category="files", description="Read a text file, with binary detection and optional offset", aliases=("find_file", "cat"))
def action_read_file(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    query = _strip(params.get("path") or params.get("query") or params.get("src") or params.get("file"))
    target = _resolve_read_target(query)
    if not target:
        return err("read_file", f"File not found: {query}", desktop=str(_desktop()))
    decision = check_path(target, "read")
    if not decision.allowed:
        return blocked("read_file", decision)
    max_chars = max(100, min(int(params.get("max_chars", 20000)), 2_000_000))
    offset = max(0, int(params.get("offset", 0)))
    try:
        raw = target.read_bytes()
        if b"\x00" in raw[:8192] and not params.get("binary"):
            return {
                "ok": False,
                "action": "read_file",
                "path": str(target),
                "binary": True,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "error": "File appears to be binary; pass binary=true to return base64 (size-limited).",
            }
        if params.get("binary"):
            data = raw[offset : offset + max_chars]
            return ok(
                "read_file",
                path=str(target),
                binary=True,
                base64=base64.b64encode(data).decode("ascii"),
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                detail=f"Binary file {target.name} ({len(raw)} bytes).",
            )
        encoding = str(params.get("encoding") or "utf-8")
        text = raw.decode(encoding, errors="replace")
        content = text[offset : offset + max_chars]
        return ok(
            "read_file",
            path=str(target),
            name=target.name,
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            preview=content,
            content=content,
            truncated=len(text) > offset + max_chars,
            detail=f"📄 **محتوى الملف `{target.name}`:**\n```\n{content}\n```",
        )
    except PermissionError:
        return err("read_file", f"Permission denied: {target}")
    except Exception as exc:
        return err("read_file", str(exc))


@register("write_file", category="files", description="Atomically write a file and verify its hash", aliases=("create_file", "save_file"))
def action_write_file(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    filename = _strip(params.get("filename") or params.get("name") or params.get("file"))
    content = params.get("content", "")
    folder_param = _strip(params.get("folder") or params.get("dest_folder"))
    explicit = _strip(params.get("path") or params.get("target") or params.get("dst"))
    if not filename and explicit:
        filename = Path(explicit).name
        folder_param = str(Path(explicit).parent)
    if not filename:
        return err("write_file", "No filename/path supplied")
    if Path(filename).is_absolute():
        target = Path(os.path.expandvars(filename)).expanduser()
    else:
        base, _ = _folder_for_write({**params, "folder": folder_param} if folder_param else params)
        if not base:
            return err("write_file", f"Destination folder not found: {folder_param}")
        target = base / filename
    decision = check_path(target, "write", approval_token=_approval(params))
    if not decision.allowed:
        return blocked("write_file", decision)
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    try:
        _write_atomic(target, data, backup=bool(params.get("backup")))
        actual = target.read_bytes()
        digest = hashlib.sha256(actual).hexdigest()
        verified = actual == data
        return ok(
            "write_file",
            path=str(target),
            name=target.name,
            bytes_written=len(data),
            sha256=digest,
            verified=verified,
            detail=f"تم إنشاء الملف `{target.name}` بنجاح في:\n`{target}`",
        ) if verified else err("write_file", "Verification failed: file content differs after write", path=str(target))
    except Exception as exc:
        return err("write_file", str(exc), path=str(target))


@register("append_file", category="files", description="Append text to a file and verify the result")
def action_append_file(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    query = _strip(params.get("path") or params.get("query") or params.get("file"))
    target = _resolve_read_target(query) or Path(os.path.expandvars(query)).expanduser()
    content = params.get("content", "")
    decision = check_path(target, "write", approval_token=_approval(params))
    if not decision.allowed:
        return blocked("append_file", decision)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(str(content))
        text = target.read_text(encoding="utf-8", errors="replace")
        return ok("append_file", path=str(target), size_bytes=target.stat().st_size, verified=str(content) in text, detail=f"Appended to `{target}`.")
    except Exception as exc:
        return err("append_file", str(exc))


@register("mkdir", category="files", description="Create a directory (and parents)", aliases=("create_folder",))
def action_mkdir(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    folder = _strip(params.get("folder") or params.get("name") or params.get("path") or params.get("src") or "NewFolder")
    parent = _strip(params.get("parent") or params.get("dest_folder") or "desktop")
    base = _resolve_folder("", parent=parent, default="desktop", create=True) if parent else _desktop()
    target = Path(os.path.expandvars(folder)).expanduser() if Path(folder).is_absolute() else (base or _desktop()) / folder
    decision = check_path(target, "write", approval_token=_approval(params))
    if not decision.allowed:
        return blocked("mkdir", decision)
    try:
        target.mkdir(parents=True, exist_ok=True)
        verified = target.is_dir()
        return ok("mkdir", path=str(target), folder_name=target.name, verified=verified, detail=f"تم إنشاء المجلد `{target.name}` بنجاح في:\n`{target}`") if verified else err("mkdir", "Directory was not created")
    except Exception as exc:
        return err("mkdir", str(exc), path=str(target))


@register("copy_file", category="files", description="Copy a file or directory and verify the destination", aliases=("copy", "cp"))
def action_copy(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    src = _strip(params.get("src") or params.get("source") or params.get("path"))
    src_parent = _strip(params.get("src_parent"))
    src_path = _resolve_existing_source(src, src_parent)
    if not src_path or not src_path.exists():
        return err("copy_file", f"Source not found: {src}", desktop=str(_desktop()))
    known = _known()
    dest_folder = _strip(params.get("dest_folder") or params.get("dest") or params.get("target_folder") or "desktop")
    dest_name = _strip(params.get("dest_name") or params.get("name"))
    if dest_folder.lower() in known:
        dest_dir = known[dest_folder.lower()]
    else:
        direct = Path(os.path.expandvars(dest_folder)).expanduser()
        if direct.is_dir():
            dest_dir = direct.resolve()
        else:
            dest_dir = (_desktop() / dest_folder).resolve()
            dest_dir.mkdir(parents=True, exist_ok=True)
    final_dest = dest_dir / (dest_name or src_path.name)
    if final_dest.resolve() == src_path.resolve():
        return err("copy_file", f"Source and destination are identical: {src_path}")
    src_decision = check_path(src_path, "copy_source")
    if not src_decision.allowed:
        return blocked("copy_file", src_decision)
    dest_decision = check_path(final_dest, "write", approval_token=_approval(params))
    if not dest_decision.allowed:
        return blocked("copy_file", dest_decision)
    try:
        if src_path.is_dir():
            shutil.copytree(str(src_path), str(final_dest), dirs_exist_ok=True)
            is_folder = True
        else:
            shutil.copy2(str(src_path), str(final_dest))
            is_folder = False
        verified = final_dest.exists()
        return ok(
            "copy_file",
            src=str(src_path),
            dest=str(final_dest),
            is_folder=is_folder,
            verified=verified,
            detail=f"تم نسخ {'المجلد' if is_folder else 'الملف'} `{src_path.name}` بنجاح إلى:\n`{final_dest}`",
        ) if verified else err("copy_file", "Copy verification failed")
    except Exception as exc:
        return err("copy_file", str(exc))


@register("move_file", category="files", description="Move or rename a file/directory and verify the result", aliases=("move", "mv", "rename"))
def action_move(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    src = _strip(params.get("src") or params.get("path") or params.get("source"))
    src_parent = _strip(params.get("src_parent"))
    src_path = _resolve_existing_source(src, src_parent)
    if not src_path:
        return err("move_file", f"Source not found: {src}")
    dest = _strip(params.get("dest") or params.get("destination") or params.get("dest_folder") or params.get("dest_name") or params.get("name"))
    if not dest:
        return err("move_file", "No destination supplied")
    known = _known()
    if "/" not in dest and "\\" not in dest and "." in dest and src_path.is_file():
        dest_path = src_path.parent / dest
    elif dest.lower() in known:
        dest_path = known[dest.lower()] / src_path.name
    else:
        direct = Path(os.path.expandvars(dest)).expanduser()
        if direct.is_dir():
            dest_path = direct / src_path.name
        elif (_desktop() / dest).is_dir():
            dest_path = (_desktop() / dest) / src_path.name
        else:
            dest_path = src_path.parent / dest
    src_decision = check_path(src_path, "move_source", approval_token=_approval(params))
    if not src_decision.allowed:
        return blocked("move_file", src_decision)
    dest_decision = check_path(dest_path, "write", approval_token=_approval(params))
    if not dest_decision.allowed:
        return blocked("move_file", dest_decision)
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dest_path))
        verified = dest_path.exists() and not src_path.exists()
        return ok(
            "move_file",
            src=str(src_path),
            dest=str(dest_path),
            verified=verified,
            detail=f"تم نقل/تعديل اسم `{src_path.name}` بنجاح إلى:\n`{dest_path}`",
        ) if verified else err("move_file", "Move verification failed")
    except Exception as exc:
        return err("move_file", str(exc))


@register("delete_path", category="files", description="Delete a file/directory with protected-path enforcement", risk="medium", aliases=("delete", "remove", "rm", "del"))
def action_delete(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    raw = _strip(params.get("path") or params.get("src") or params.get("target") or params.get("folder") or params.get("query"))
    if not raw:
        return err("delete_path", "No path supplied")
    known = _known()
    desktop = known.get("desktop", Path.home() / "Desktop")
    candidates = [
        (desktop / raw).resolve(),
        Path(os.path.expandvars(raw)).expanduser().resolve(),
        *[(base / raw).resolve() for base in known.values()],
        (Path.home() / raw).resolve(),
        (PROJECT_ROOT / raw).resolve(),
    ]
    target = next((p for p in candidates if p.exists()), None)
    if target is None:
        target_name = Path(raw).name.lower()
        for root, dirs, files in os.walk(desktop):
            rel = Path(root).relative_to(desktop)
            if len(rel.parts) > 2:
                dirs.clear()
                continue
            for name in dirs + files:
                if name.lower() == target_name:
                    target = (Path(root) / name).resolve()
                    break
            if target:
                break
    if not target or not target.exists():
        return err("delete_path", f"Target not found: {raw}")
    decision = check_path(target, "delete", approval_token=_approval(params))
    if not decision.allowed:
        return blocked("delete_path", decision)
    try:
        is_dir = target.is_dir()
        if is_dir:
            def _onerror(func: Any, path: str, exc_info: Any) -> None:
                try:
                    os.chmod(path, 0o700)
                    func(path)
                except Exception:
                    pass

            shutil.rmtree(target, onerror=_onerror)
        else:
            try:
                target.unlink()
            except PermissionError:
                os.chmod(str(target), 0o600)
                target.unlink()
        verified = not target.exists()
        return ok(
            "delete_path",
            path=str(target),
            name=Path(target).name,
            is_folder=is_dir,
            verified=verified,
            detail=f"تم حذف {'المجلد' if is_dir else 'الملف'} `{Path(target).name}` بنجاح من حاسوبك الشخصي.\n📁 المسار المحذوف: `{target}`",
        ) if verified else err("delete_path", "Delete verification failed; path still exists")
    except Exception as exc:
        return err("delete_path", str(exc), path=str(target))


@register("stat_path", category="files", description="File metadata and SHA-256", aliases=("stat", "file_info"))
def action_stat(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    raw = _strip(params.get("path") or params.get("query") or params.get("src"))
    target = Path(os.path.expandvars(raw)).expanduser() if raw else None
    if target and not target.exists():
        target = _resolve_read_target(raw)
    if not target or not target.exists():
        return err("stat_path", f"Path not found: {raw}")
    decision = check_path(target, "read")
    if not decision.allowed:
        return blocked("stat_path", decision)
    stat = target.stat()
    data = {
        "path": str(target),
        "name": target.name,
        "is_file": target.is_file(),
        "is_dir": target.is_dir(),
        "size_bytes": stat.st_size if target.is_file() else None,
        "modified_at": stat.st_mtime,
        "created_at": stat.st_ctime,
        "mode": oct(stat.st_mode),
    }
    if target.is_file():
        data["sha256"] = _file_sha256(target)
    return ok("stat_path", **data, detail=f"Metadata for {target.name}.")


@register("glob_files", category="files", description="Find files by glob pattern", aliases=("glob", "find_files"))
def action_glob(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    root_raw = _strip(params.get("root") or params.get("cwd") or params.get("folder")) or str(PROJECT_ROOT)
    root = Path(os.path.expandvars(root_raw)).expanduser().resolve()
    pattern = _strip(params.get("pattern") or params.get("glob") or "*")
    if not root.is_dir():
        return err("glob_files", f"Root is not a directory: {root}")
    decision = check_path(root, "read")
    if not decision.allowed:
        return blocked("glob_files", decision)
    limit = max(1, min(int(params.get("limit", 500)), 5000))
    rows: list[dict[str, Any]] = []
    try:
        for path in root.glob(pattern):
            if any(part in {".git", ".venv", "node_modules", "__pycache__"} for part in path.parts):
                continue
            if not check_path(path, "read").allowed:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append({"path": str(path), "name": path.name, "type": "directory" if path.is_dir() else "file", "size_bytes": None if path.is_dir() else stat.st_size})
            if len(rows) >= limit:
                break
    except Exception as exc:
        return err("glob_files", str(exc))
    return ok("glob_files", root=str(root), pattern=pattern, files=rows, count=len(rows), detail=f"{len(rows)} path(s) matched '{pattern}'.")


@register("search_text", category="files", description="Search text/regex across files (coding and research helper)", aliases=("grep", "search"))
def action_search_text(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    root_raw = _strip(params.get("root") or params.get("folder") or params.get("path")) or str(PROJECT_ROOT)
    root = Path(os.path.expandvars(root_raw)).expanduser().resolve()
    pattern = str(params.get("pattern") or params.get("query") or "")
    if not pattern:
        return err("search_text", "Provide 'pattern'")
    if not root.exists():
        return err("search_text", f"Root not found: {root}")
    decision = check_path(root, "read")
    if not decision.allowed:
        return blocked("search_text", decision)
    literal = bool(params.get("literal"))
    flags = re.MULTILINE
    if not params.get("case_sensitive"):
        flags |= re.IGNORECASE
    try:
        regex = re.compile(re.escape(pattern) if literal else pattern, flags)
    except re.error as exc:
        return err("search_text", f"Invalid regex: {exc}")
    include = str(params.get("include") or "*")
    exclude_dirs = set(params.get("exclude_dirs") or [".git", ".venv", "node_modules", "__pycache__", "dist", "build", ".pytest_cache"])
    max_results = max(1, min(int(params.get("max_results", 100)), 1000))
    max_file_bytes = max(1024, min(int(params.get("max_file_bytes", 2_000_000)), 20_000_000))
    context_lines = max(0, min(int(params.get("context_lines", 0)), 5))
    matches: list[dict[str, Any]] = []
    files_scanned = 0
    root_is_file = root.is_file()
    paths = [root] if root_is_file else None
    try:
        iterator = paths or root.rglob(include)
        for file_path in iterator:
            ctx.check()
            if file_path.is_dir():
                continue
            if any(part in exclude_dirs for part in file_path.parts):
                continue
            if not check_path(file_path, "read").allowed:
                continue
            try:
                if file_path.stat().st_size > max_file_bytes:
                    continue
                raw = file_path.read_bytes()
                if b"\x00" in raw[:2048]:
                    continue
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            files_scanned += 1
            lines = text.splitlines()
            for lineno, line in enumerate(lines, 1):
                if regex.search(line):
                    row = {"path": str(file_path), "line": lineno, "text": line[:1000]}
                    if context_lines:
                        start = max(0, lineno - 1 - context_lines)
                        end = min(len(lines), lineno + context_lines)
                        row["context"] = lines[start:end]
                    matches.append(row)
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
    except Exception as exc:
        return err("search_text", str(exc))
    return ok("search_text", root=str(root), pattern=pattern, matches=matches, count=len(matches), files_scanned=files_scanned, detail=f"{len(matches)} match(es) in {files_scanned} file(s).")


@register("replace_in_file", category="files", description="Replace exact text in a file with count verification and optional backup", risk="medium")
def action_replace_in_file(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    raw = _strip(params.get("path") or params.get("file") or params.get("query"))
    target = _resolve_read_target(raw) or Path(os.path.expandvars(raw)).expanduser()
    old = params.get("old")
    new = params.get("new", "")
    if old is None:
        return err("replace_in_file", "Provide 'old' text")
    decision = check_path(target, "write", approval_token=_approval(params))
    if not decision.allowed:
        return blocked("replace_in_file", decision)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
        count = text.count(str(old))
        expected = params.get("expected_count")
        if count == 0:
            return err("replace_in_file", "Old text was not found", path=str(target))
        if expected is not None and count != int(expected):
            return err("replace_in_file", f"Expected {expected} occurrence(s), found {count}", path=str(target))
        _write_atomic(target, text.replace(str(old), str(new)), backup=bool(params.get("backup", True)))
        updated = target.read_text(encoding="utf-8", errors="replace")
        verified = str(new) in updated and (str(old) not in updated if old != new else True)
        return ok("replace_in_file", path=str(target), replacements=count, verified=verified, sha256=_file_sha256(target), detail=f"Replaced {count} occurrence(s) in `{target.name}`.") if verified else err("replace_in_file", "Post-write verification failed", path=str(target))
    except Exception as exc:
        return err("replace_in_file", str(exc))


@register("archive_create", category="files", description="Create a zip/tar archive from files or directories")
def action_archive_create(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    sources = params.get("sources") or params.get("paths") or params.get("source")
    if isinstance(sources, str):
        sources = [sources]
    if not sources:
        return err("archive_create", "Provide 'sources'")
    out = _strip(params.get("output") or params.get("dest"))
    if not out:
        return err("archive_create", "Provide 'output' path")
    out_path = Path(os.path.expandvars(out)).expanduser()
    if not out_path.is_absolute():
        out_path = _desktop() / out_path
    decision = check_path(out_path, "write", approval_token=_approval(params))
    if not decision.allowed:
        return blocked("archive_create", decision)
    src_paths = [Path(os.path.expandvars(str(s))).expanduser() for s in sources]
    # Archiving reads file content, so every member must pass the read policy
    # (otherwise a protected credential hive could be exfiltrated in a zip).
    for src in src_paths:
        src_decision = check_path(src, "read")
        if not src_decision.allowed:
            return blocked("archive_create", src_decision)
    missing = [str(p) for p in src_paths if not p.exists()]
    if missing:
        return err("archive_create", f"Missing sources: {missing}")
    for src in src_paths:
        if src.is_dir():
            for member in src.rglob("*"):
                if not member.is_file():
                    continue
                member_decision = check_path(member, "read")
                if not member_decision.allowed:
                    return blocked("archive_create", member_decision)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if out_path.suffix.lower() == ".zip":
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for src in src_paths:
                    if src.is_dir():
                        for file in src.rglob("*"):
                            if file.is_file():
                                zf.write(file, file.relative_to(src.parent))
                    else:
                        zf.write(src, src.name)
        elif out_path.suffix.lower() in (".tar", ".gz", ".tgz", ".bz2", ".xz"):
            mode = "w:gz" if out_path.suffix.lower() in (".gz", ".tgz") else "w"
            with tarfile.open(out_path, mode) as tf:
                for src in src_paths:
                    tf.add(src, arcname=src.name)
        else:
            return err("archive_create", "Archive extension must be .zip/.tar/.gz/.tgz/.bz2/.xz")
        verified = out_path.exists() and out_path.stat().st_size > 0
        return ok("archive_create", path=str(out_path), size_bytes=out_path.stat().st_size, verified=verified, detail=f"Archive created: `{out_path}`") if verified else err("archive_create", "Archive verification failed")
    except Exception as exc:
        return err("archive_create", str(exc))


@register("archive_extract", category="files", description="Safely extract a zip/tar archive (blocks path traversal)", risk="medium")
def action_archive_extract(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    raw = _strip(params.get("path") or params.get("archive"))
    src = Path(os.path.expandvars(raw)).expanduser()
    if not src.is_file():
        return err("archive_extract", f"Archive not found: {raw}")
    dest_raw = _strip(params.get("dest") or params.get("destination") or params.get("folder"))
    dest = Path(os.path.expandvars(dest_raw)).expanduser() if dest_raw else src.with_suffix("")
    if not dest.is_absolute():
        dest = _desktop() / dest
    decision = check_path(dest, "write", approval_token=_approval(params))
    if not decision.allowed:
        return blocked("archive_extract", decision)
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()

    def _safe_member(name: str) -> Path | None:
        try:
            candidate = (dest_resolved / name).resolve()
            candidate.relative_to(dest_resolved)
            return candidate
        except (ValueError, OSError):
            return None

    try:
        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src) as zf:
                for member in zf.infolist():
                    if _safe_member(member.filename) is None:
                        return err("archive_extract", f"Unsafe archive member: {member.filename}")
                zf.extractall(dest_resolved)
        elif tarfile.is_tarfile(src):
            with tarfile.open(src) as tf:
                for member in tf.getmembers():
                    if _safe_member(member.name) is None or member.issym() or member.islnk():
                        return err("archive_extract", f"Unsafe archive member: {member.name}")
                tf.extractall(dest_resolved)
        else:
            return err("archive_extract", "Unsupported archive format")
        return ok("archive_extract", path=str(dest_resolved), verified=dest_resolved.is_dir(), detail=f"Archive extracted to `{dest_resolved}`.")
    except Exception as exc:
        return err("archive_extract", str(exc))


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


@register("process_start", category="processes", description="Start a managed background process with logs", risk="medium", aliases=("start_process",))
def action_process_start(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    command = params.get("cmd") or params.get("command")
    if not command:
        return err("process_start", "No command supplied")
    decision = check_command(command, cwd=params.get("cwd"), approval_token=_approval(params))
    if not decision.allowed:
        return blocked("process_start", decision)
    try:
        proc = process_manager.start(
            command,
            cwd=params.get("cwd") or PROJECT_ROOT,
            env=params.get("env") if isinstance(params.get("env"), dict) else None,
            name=params.get("name"),
            shell=bool(params.get("shell", not isinstance(command, list))),
            hidden=bool(params.get("hidden", True)),
        )
        time.sleep(float(params.get("startup_grace", 0.2)))
        running = proc.running()
        return ok(
            "process_start",
            process_id=proc.process_id,
            pid=proc.pid,
            running=running,
            log_path=proc.metadata.get("log_path"),
            verified=pid_alive(int(proc.pid or 0)),
            detail=f"Started `{proc.process_id}` (PID {proc.pid}).",
        )
    except Exception as exc:
        return err("process_start", str(exc))


@register("process_list", category="processes", description="List managed or system processes", aliases=("ps",))
def action_process_list(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    rows = process_manager.list(include_all=bool(params.get("all")))
    return ok("process_list", processes=rows, count=len(rows), detail=f"{len(rows)} process(es).")


@register("process_status", category="processes", description="Status of a managed process")
def action_process_status(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    pid_or_id = _strip(params.get("process_id") or params.get("pid") or params.get("id"))
    proc = process_manager.get(pid_or_id)
    if not proc:
        return err("process_status", f"Unknown process: {pid_or_id}")
    return ok("process_status", process_id=proc.process_id, pid=proc.pid, running=proc.running(), exit_code=proc.exit_code(), command=proc.metadata.get("command"), log_path=proc.metadata.get("log_path"))


@register("process_logs", category="processes", description="Read logs from a managed process", aliases=("process_output",))
def action_process_logs(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    pid_or_id = _strip(params.get("process_id") or params.get("pid") or params.get("id"))
    return process_manager.logs(pid_or_id, lines=int(params.get("lines", 100)))


@register("process_stop", category="processes", description="Stop a managed process and its children", risk="medium", aliases=("kill_process",))
def action_process_stop(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    pid_or_id = _strip(params.get("process_id") or params.get("pid") or params.get("id"))
    result = process_manager.stop(pid_or_id, timeout=float(params.get("timeout", 10)))
    result.setdefault("action", "process_stop")
    return result


@register("process_wait", category="processes", description="Wait for a managed process to finish")
def action_process_wait(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    pid_or_id = _strip(params.get("process_id") or params.get("pid") or params.get("id"))
    result = process_manager.wait(pid_or_id, timeout=min(float(params.get("timeout", 30)), 3600))
    result.setdefault("action", "process_wait")
    return result


# ---------------------------------------------------------------------------
# HTTP / APIs
# ---------------------------------------------------------------------------


def _validate_url(url: str) -> str | None:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return f"Invalid URL: {exc}"
    if parsed.scheme not in ("http", "https"):
        return "Only http:// and https:// URLs are allowed"
    host = (parsed.hostname or "").lower()
    if not host:
        return "URL has no host"
    if host in ("169.254.169.254", "metadata.google.internal", "metadata.azure.com") or host.startswith("169.254."):
        return "Cloud metadata endpoints are blocked"
    return None


@register("http_request", category="network", description="Call an external/local HTTP API with size and timeout limits", risk="medium", aliases=("http", "api_call"))
def action_http_request(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    import httpx

    url = str(params.get("url") or "").strip()
    problem = _validate_url(url)
    if problem:
        return err("http_request", problem)
    method = str(params.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return err("http_request", f"Unsupported method: {method}")
    timeout = clamp_timeout(params.get("timeout", 60), default=60, maximum=600)
    headers = {str(k): str(v) for k, v in (params.get("headers") or {}).items()}
    verify_tls = params.get("verify_tls", True)
    if not verify_tls and not approval_valid(_approval(params)):
        return {
            "ok": False,
            "action": "http_request",
            "status": "blocked",
            "blocked": True,
            "error": "verify_tls=false requires BRIDGE_APPROVAL_TOKEN and a valid approval_token",
        }
    max_bytes = max(1024, min(int(params.get("max_response_bytes", 2_000_000)), 20_000_000))
    retries = max(0, min(int(params.get("retries", 0) or 0), 2)) if method in ("GET", "HEAD") else 0
    last_error = ""
    for attempt in range(retries + 1):
        ctx.check()
        try:
            started = time.time()
            with httpx.Client(follow_redirects=bool(params.get("follow_redirects", True)), verify=bool(verify_tls), timeout=timeout) as client:
                response = client.request(
                    method,
                    url,
                    params=params.get("params") if isinstance(params.get("params"), dict) else None,
                    json=params.get("json"),
                    data=params.get("data") if isinstance(params.get("data"), (dict, str)) else None,
                    headers=headers,
                    content=base64.b64decode(params["body_base64"]) if params.get("body_base64") else None,
                )
            body_bytes = response.content[:max_bytes]
            text = body_bytes.decode(response.encoding or "utf-8", errors="replace")
            parsed: Any = None
            content_type = response.headers.get("content-type", "")
            if "json" in content_type or text.strip().startswith(("{", "[")):
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = None
            data = {
                "ok": response.is_success,
                "action": "http_request",
                "status": "completed" if response.is_success else "failed",
                "status_code": response.status_code,
                "url": redact(url, max_chars=2000),
                "method": method,
                "headers": {k: v for k, v in response.headers.items() if k.lower() in ("content-type", "content-length", "date", "server", "location", "etag")},
                "body": redact(text, max_chars=max_bytes),
                "text": redact(text, max_chars=max_bytes),
                "json": parsed,
                "elapsed_ms": int((time.time() - started) * 1000),
                "truncated": len(response.content) > max_bytes,
                "verified": True,
                "detail": f"HTTP {method} {url} -> {response.status_code}",
            }
            if not response.is_success:
                data["error"] = f"HTTP {response.status_code}"
            return data
        except Exception as exc:
            last_error = str(exc)
            if attempt < retries:
                if ctx.cancellation.wait(1.0 + attempt):
                    break
                continue
    return err("http_request", last_error or "HTTP request failed")


@register("download_file", category="network", description="Download a URL to a local path with size limits and SHA-256 verification", risk="medium")
def action_download_file(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    import httpx

    url = str(params.get("url") or "").strip()
    problem = _validate_url(url)
    if problem:
        return err("download_file", problem)
    raw = _strip(params.get("path") or params.get("dest") or params.get("filename"))
    if not raw:
        return err("download_file", "Provide a destination path")
    target = Path(os.path.expandvars(raw)).expanduser()
    if not target.is_absolute():
        target = (_resolve_folder(_strip(params.get("folder")), default="downloads", create=True) or _desktop()) / target
    decision = check_path(target, "write", approval_token=_approval(params))
    if not decision.allowed:
        return blocked("download_file", decision)
    target.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = max(1024, min(int(params.get("max_bytes", 100_000_000)), 2_000_000_000))
    timeout = clamp_timeout(params.get("timeout", 300), default=300, maximum=3600)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True, headers=params.get("headers") or {}) as response:
            response.raise_for_status()
            written = 0
            with tmp.open("wb") as fh:
                for chunk in response.iter_bytes(65_536):
                    ctx.check()
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(f"Download exceeds max_bytes={max_bytes}")
                    fh.write(chunk)
        os.replace(tmp, target)
        digest = _file_sha256(target)
        expected = str(params.get("sha256") or "").lower()
        verified = target.exists() and target.stat().st_size > 0 and (not expected or digest == expected)
        return ok(
            "download_file",
            path=str(target),
            size_bytes=target.stat().st_size,
            sha256=digest,
            verified=verified,
            detail=f"Downloaded `{target.name}` ({target.stat().st_size} bytes).",
        ) if verified else err("download_file", "Download verification failed", path=str(target))
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return err("download_file", str(exc))


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


@register("git", category="development", description="Run a git subcommand safely in a repository", risk="variable", aliases=("git_cmd",))
def action_git(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    cwd = Path(os.path.expandvars(str(params.get("cwd") or PROJECT_ROOT))).expanduser().resolve()
    if not (cwd / ".git").exists():
        # Allow worktrees where .git is a file.
        if not (cwd / ".git").is_file() and not params.get("allow_non_repo"):
            return err("git", f"Not a git repository: {cwd}")
    subcommand = str(params.get("subcommand") or params.get("op") or params.get("command") or "").strip()
    args = params.get("args") or []
    if isinstance(args, str):
        args = [args]
    argv = ["git", subcommand, *[str(a) for a in args]]
    decision = check_command(argv, cwd=cwd, approval_token=_approval(params))
    if not decision.allowed:
        return blocked("git", decision)
    result = run_process(argv, cwd=cwd, timeout=clamp_timeout(params.get("timeout", 120)), shell=False, env=params.get("env") if isinstance(params.get("env"), dict) else None, token=ctx.cancellation)
    data = result.as_dict()
    data.update({"action": "git", "subcommand": subcommand, "security": decision.as_dict()})
    data["status"] = "completed" if result.ok else "failed"
    data["verified"] = result.ok
    return data


# ---------------------------------------------------------------------------
# Workflows and cloud routing
# ---------------------------------------------------------------------------


@register("workflow", category="orchestration", description="Run a durable multi-step workflow with parallelism, retries and checkpoints", aliases=("batch", "pipeline", "run_workflow"))
def action_workflow(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    spec = params.get("workflow") if isinstance(params.get("workflow"), dict) else params
    if not isinstance(spec, dict) or "steps" not in spec:
        return err("workflow", "Provide a 'steps' list (or a 'workflow' object)")
    result = run_workflow(spec, execute=lambda action, p, c: executor.dispatch(action, p, c), context=ctx)
    return result


@register("plan_execution", category="orchestration", description="Choose local or cloud execution based on capacity, cost and requirements")
def action_plan_execution(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    plan = cloud_mod.planner.plan(params)
    return {"action": "plan_execution", **plan, "status": "completed" if plan.get("ok") else "failed", "detail": plan.get("reason", "")}


@register("cloud_providers", category="cloud", description="List configured cloud providers and availability")
def action_cloud_providers(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    info = cloud_mod.cloud_providers_info(check_availability=bool(params.get("check", False)))
    return ok("cloud_providers", **info, detail=f"{info['count']} cloud provider(s) configured.")


@register("cloud_execute", category="cloud", description="Delegate code/commands to cloud infrastructure, or execute locally when sufficient", risk="medium", aliases=("delegate",))
def action_cloud_execute(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    requirements = params.get("requirements") if isinstance(params.get("requirements"), dict) else {}
    for key in ("gpu", "min_cpu", "cpu_cores", "min_memory_gb", "memory_gb", "min_disk_gb", "max_cost_usd", "prefer", "target", "priority", "requires_cloud", "timeout_seconds", "timeout"):
        if key in params and key not in requirements:
            requirements[key] = params[key]
    plan = cloud_mod.planner.plan(requirements)
    if not plan.get("ok"):
        return {"action": "cloud_execute", "status": "unavailable", "ok": False, "plan": plan, "error": plan.get("reason", "No execution target available")}

    target = plan["target"]
    job = {
        "command": params.get("command") or params.get("cmd"),
        "code": params.get("code"),
        "language": params.get("language", "python"),
        "files": params.get("files") or {},
        "timeout": clamp_timeout(params.get("timeout", 300), default=300, maximum=7200),
        "gpu": bool(requirements.get("gpu")),
        "remote_workdir": params.get("remote_workdir"),
    }
    if target == "local":
        ctx.progress(message="Executing locally (capacity is sufficient)", force=True)
        action = "run_code" if job.get("code") else "run_command"
        local_params = {
            "code": job["code"],
            "language": job["language"],
            "files": job["files"],
            "cmd": job["command"],
            "timeout": job["timeout"],
            "cwd": params.get("cwd"),
            "approval_token": _approval(params),
            "keep_workspace": params.get("keep_workspace", False),
        }
        result = executor.dispatch(action, local_params, ctx.child("local-execution"))
        return {**result, "action": "cloud_execute", "execution_target": "local", "plan": plan, "delegated": False}

    provider = cloud_mod.providers.get(target)
    if not provider:
        return err("cloud_execute", f"Cloud provider not found: {target}", plan=plan)
    ctx.progress(message=f"Delegating to cloud provider {target}", force=True)
    started = time.time()
    result = provider.submit(job, token=ctx.cancellation)
    result.setdefault("ok", False)
    result.update(
        {
            "action": "cloud_execute",
            "execution_target": "cloud",
            "provider": target,
            "delegated": True,
            "plan": plan,
            "elapsed_seconds": round(time.time() - started, 2),
            "status": "completed" if result.get("ok") else "failed",
            "verified": bool(result.get("ok")),
            "detail": result.get("detail") or result.get("stdout") or result.get("error", ""),
        }
    )
    return redact_obj(result)


@register("verify", category="verification", description="Verify a result using exit code, stdout, JSON, HTTP status or file checks")
def action_verify(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    result = params.get("result")
    if not isinstance(result, dict):
        action = params.get("verify_action") or params.get("action_to_run")
        action_params = params.get("params") or {}
        if action:
            result = executor.dispatch(str(action), action_params if isinstance(action_params, dict) else {}, ctx.child("verify-target"))
        else:
            result = {}
    checks = params.get("checks") or params.get("expect")
    if not checks:
        return err("verify", "Provide 'checks' (and optionally 'result')")
    verification = verify_result(result, checks)
    return {
        "ok": verification.get("ok", False),
        "action": "verify",
        "status": "completed" if verification.get("ok") else "failed",
        "verification": verification,
        "verified": verification.get("ok", False),
        "result_checked": redact_obj(result) if params.get("include_result") else None,
        "detail": "Verification passed." if verification.get("ok") else "Verification failed.",
    }


# ---------------------------------------------------------------------------
# Automation (browser, mail, scraping, outreach)
# ---------------------------------------------------------------------------


@register("browser_scrape", category="automation", description="Scrape a web page with optional CSS selector (Playwright optional)", risk="medium")
def action_browser_scrape(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    from app.automation.browser import BrowserAutomationEngine

    engine = BrowserAutomationEngine(headless=bool(params.get("headless", True)), browser=str(params.get("browser") or "brave"))
    result = engine.scrape_page(str(params.get("url") or ""), params.get("selector"), int(params.get("timeout_ms", 30000)))
    result.setdefault("action", "browser_scrape")
    return result


@register("email_read", category="automation", description="Read unread emails through IMAP", risk="medium")
def action_email_read(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    from app.automation.mail import MailAutomation

    mailer = MailAutomation(
        str(params.get("imap_host") or ""),
        str(params.get("smtp_host") or params.get("imap_host") or ""),
        str(params.get("username") or ""),
        str(params.get("password") or ""),
        int(params.get("imap_port", 993)),
    )
    result = mailer.list_unread(str(params.get("folder", "INBOX")), int(params.get("limit", 20)))
    result.setdefault("action", "email_read")
    return result


@register("email_reply", category="automation", description="Send an email reply through SMTP", risk="medium")
def action_email_reply(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    from app.automation.mail import MailAutomation

    mailer = MailAutomation(
        str(params.get("imap_host") or params.get("smtp_host") or ""),
        str(params.get("smtp_host") or ""),
        str(params.get("username") or ""),
        str(params.get("password") or ""),
        smtp_port=int(params.get("smtp_port", 587)),
    )
    result = mailer.reply(str(params.get("to") or ""), str(params.get("subject") or ""), str(params.get("body") or ""), params.get("from_address"))
    result.setdefault("action", "email_reply")
    return result


@register("lead_scrape", category="automation", description="Fetch public pages and extract emails/phones", risk="medium")
def action_lead_scrape(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    from app.automation.scraper import LeadScraper

    urls = params.get("urls") or params.get("url") or []
    if isinstance(urls, str):
        urls = [urls]
    result = LeadScraper(timeout=int(params.get("timeout", 20))).fetch_leads_from_urls(list(urls))
    result.setdefault("action", "lead_scrape")
    return result


@register("outreach_campaign", category="automation", description="Generate or send outreach proposals from scraped leads", risk="high")
def action_outreach_campaign(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    from app.automation.mail import MailAutomation
    from app.automation.outreach import OutreachEngine

    mailer = MailAutomation(
        str(params.get("imap_host") or params.get("smtp_host") or ""),
        str(params.get("smtp_host") or ""),
        str(params.get("username") or ""),
        str(params.get("password") or ""),
        smtp_port=int(params.get("smtp_port", 587)),
    )
    engine = OutreachEngine(mailer)
    leads = params.get("leads") or []
    result = engine.send_campaign(list(leads), dry_run=bool(params.get("dry_run", True)))
    result.setdefault("action", "outreach_campaign")
    return result


@register("wait", category="general", description="Wait for a bounded duration (useful between workflow steps)")
def action_wait(params: dict[str, Any], ctx: ExecutionContext, executor: Any) -> dict[str, Any]:
    seconds = max(0.0, min(float(params.get("seconds", params.get("timeout", 1))), 300.0))
    if ctx.cancellation.wait(seconds):
        return {"ok": False, "action": "wait", "status": "cancelled", "cancelled": True, "error": "Cancelled while waiting"}
    return ok("wait", seconds=seconds, detail=f"Waited {seconds}s.")
