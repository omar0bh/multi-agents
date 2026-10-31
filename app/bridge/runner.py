"""Process execution, cancellation, resource monitoring and managed processes."""

from __future__ import annotations

import codecs
import ctypes
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from app.bridge.security import redact, sanitized_env

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROCESS_DIR = PROJECT_ROOT / "data" / "bridge_processes"
DEFAULT_MAX_OUTPUT = int(os.getenv("BRIDGE_MAX_OUTPUT_CHARS", "100000"))
DEFAULT_TIMEOUT = int(os.getenv("BRIDGE_DEFAULT_TIMEOUT", "120"))
MAX_TIMEOUT = int(os.getenv("BRIDGE_MAX_TIMEOUT", "3600"))

IS_WINDOWS = os.name == "nt"


class ExecutionCancelled(RuntimeError):
    pass


class CancellationToken:
    def __init__(self, task_id: str | None = None):
        self.task_id = task_id
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "cancelled") -> None:
        # Preserve the first cancellation reason; repeated requests are
        # idempotent so a later generic reason cannot mask the real one.
        if not self._event.is_set():
            self.reason = reason or "cancelled"
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ExecutionCancelled(self.reason)

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


class CancellationRegistry:
    """Maps task ids to live cancellation tokens."""

    def __init__(self) -> None:
        self._tokens: dict[str, CancellationToken] = {}
        self._lock = threading.RLock()

    def register(self, task_id: str, token: CancellationToken | None = None) -> CancellationToken:
        token = token or CancellationToken(task_id)
        token.task_id = task_id
        with self._lock:
            self._tokens[task_id] = token
        return token

    def get(self, task_id: str) -> CancellationToken | None:
        with self._lock:
            return self._tokens.get(task_id)

    def cancel(self, task_id: str, reason: str = "cancelled by user") -> bool:
        with self._lock:
            token = self._tokens.get(task_id)
        if token:
            token.cancel(reason)
            return True
        return False

    def unregister(self, task_id: str) -> None:
        with self._lock:
            self._tokens.pop(task_id, None)

    def active(self) -> list[str]:
        with self._lock:
            return sorted(self._tokens)

    def cancel_all(self, reason: str = "bridge shutting down") -> int:
        with self._lock:
            tokens = list(self._tokens.values())
        for token in tokens:
            token.cancel(reason)
        return len(tokens)


cancellations = CancellationRegistry()


def clamp_timeout(value: Any, default: int = DEFAULT_TIMEOUT, maximum: int = MAX_TIMEOUT) -> int:
    try:
        t = int(float(value))
    except (TypeError, ValueError):
        t = default
    return max(1, min(t, maximum))


def truncate_output(text: str, limit: int = DEFAULT_MAX_OUTPUT) -> tuple[str, bool]:
    if text is None:
        return "", False
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = int(limit * 0.7)
    tail = limit - head
    return text[:head] + f"\n... [truncated, {len(text) - limit} chars omitted] ...\n" + text[-tail:], True


def _decode(data: bytes) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", sys.getfilesystemencoding() or "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _creation_flags(hidden: bool = True, new_group: bool = True) -> int:
    if not IS_WINDOWS:
        return 0
    flags = 0
    if hidden:
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if new_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    return flags


def kill_process_tree(pid: int, *, force: bool = True) -> None:
    if not pid or pid <= 0:
        return
    try:
        if IS_WINDOWS:
            args = ["taskkill", "/PID", str(pid), "/T"]
            if force:
                args.append("/F")
            subprocess.run(args, capture_output=True, timeout=15, creationflags=_creation_flags())
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except Exception as exc:
        logger.debug("Failed to kill process tree %s: %s", pid, exc)


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError):
        return False


@dataclass
class RunResult:
    ok: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    error: str = ""
    pid: int | None = None
    command: str = ""
    cwd: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "error": self.error,
            "pid": self.pid,
            "command": redact(self.command, max_chars=4000),
            "cwd": self.cwd,
            "metadata": redact(self.metadata) if self.metadata else {},
        }


def run_process(
    command: str | Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: int | float = DEFAULT_TIMEOUT,
    shell: bool = True,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    max_output: int = DEFAULT_MAX_OUTPUT,
    token: CancellationToken | None = None,
    hidden: bool = True,
    detach: bool = False,
) -> RunResult:
    """Run a foreground process with timeout, cancellation and output caps."""
    started = time.time()
    cwd_path = Path(cwd).expanduser().resolve() if cwd else PROJECT_ROOT
    if not cwd_path.is_dir():
        return RunResult(False, error=f"Working directory does not exist: {cwd_path}", cwd=str(cwd_path))

    command_text = command if isinstance(command, str) else subprocess.list2cmdline([str(c) for c in command])
    if not hidden and IS_WINDOWS:
        creationflags = _creation_flags(hidden=False, new_group=True)
    else:
        creationflags = _creation_flags(hidden=hidden, new_group=True)

    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd_path),
        "env": sanitized_env(env),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        "shell": shell,
        "text": False,
        "creationflags": creationflags if IS_WINDOWS else 0,
    }
    if not IS_WINDOWS:
        popen_kwargs["start_new_session"] = True

    if isinstance(command, str):
        # On Windows, an explicit cmd.exe shell is more predictable than
        # subprocess' implicit shell handling.
        if IS_WINDOWS and shell:
            popen_kwargs["executable"] = os.environ.get("COMSPEC", "cmd.exe")
        popen_kwargs["args"] = command
    else:
        popen_kwargs["args"] = [str(c) for c in command]

    try:
        proc = subprocess.Popen(**popen_kwargs)
    except FileNotFoundError as exc:
        return RunResult(False, error=f"Executable not found: {exc}", command=command_text, cwd=str(cwd_path))
    except OSError as exc:
        return RunResult(False, error=str(exc), command=command_text, cwd=str(cwd_path))

    if detach:
        # Caller takes ownership of the process handle/log files.
        return RunResult(True, pid=proc.pid, command=command_text, cwd=str(cwd_path), metadata={"detached": True})

    out = b""
    err = b""
    timed_out = False
    cancelled = False
    deadline = started + max(1.0, float(timeout))
    send_input = input_text.encode("utf-8") if input_text is not None else None
    try:
        while True:
            if token and token.cancelled:
                cancelled = True
                kill_process_tree(proc.pid)
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                kill_process_tree(proc.pid)
                break
            try:
                out, err = proc.communicate(input=send_input, timeout=min(0.5, remaining))
                break
            except subprocess.TimeoutExpired:
                # Input can only be delivered once; resending it after a
                # timeout raises "Cannot send input after starting communication".
                send_input = None
                continue
    except Exception as exc:
        kill_process_tree(proc.pid)
        return RunResult(False, error=str(exc), command=command_text, cwd=str(cwd_path))
    finally:
        if timed_out or cancelled:
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:
                pass

    stdout = redact(_decode(out), max_chars=max_output * 2)
    stderr = redact(_decode(err), max_chars=max_output * 2)
    stdout, t1 = truncate_output(stdout, max_output)
    stderr, t2 = truncate_output(stderr, max_output)
    duration_ms = int((time.time() - started) * 1000)
    returncode = proc.returncode
    ok = (not timed_out and not cancelled and returncode == 0)
    error = ""
    if timed_out:
        error = f"Process timed out after {timeout}s and was terminated."
    elif cancelled:
        error = f"Process cancelled{(': ' + token.reason) if token and token.reason else ''}."
    elif returncode != 0:
        error = f"Process exited with code {returncode}."
    return RunResult(
        ok,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=timed_out,
        cancelled=cancelled,
        truncated=t1 or t2,
        error=error,
        pid=proc.pid,
        command=command_text,
        cwd=str(cwd_path),
    )


# ---------------------------------------------------------------------------
# Managed background processes
# ---------------------------------------------------------------------------


def _process_meta_path(process_id: str) -> Path:
    return PROCESS_DIR / f"{process_id}.json"


def _process_log_path(process_id: str) -> Path:
    return PROCESS_DIR / f"{process_id}.log"


class ManagedProcess:
    def __init__(self, process_id: str, popen: subprocess.Popen | None, metadata: dict[str, Any]):
        self.process_id = process_id
        self.popen = popen
        self.metadata = metadata

    @property
    def pid(self) -> int | None:
        if self.popen is not None:
            return self.popen.pid
        try:
            return int(self.metadata.get("pid") or 0) or None
        except (TypeError, ValueError):
            return None

    def running(self) -> bool:
        if self.popen is not None and self.popen.poll() is None:
            return True
        return pid_alive(int(self.metadata.get("pid") or 0)) if self.pid else False

    def exit_code(self) -> int | None:
        if self.popen is not None:
            return self.popen.poll()
        return self.metadata.get("exit_code")


class ProcessManager:
    def __init__(self) -> None:
        self._procs: dict[str, ManagedProcess] = {}
        self._lock = threading.RLock()
        PROCESS_DIR.mkdir(parents=True, exist_ok=True)
        self._load_metadata()

    def _load_metadata(self) -> None:
        for meta_file in PROCESS_DIR.glob("*.json"):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                pid = int(meta.get("pid") or 0)
                if pid and pid_alive(pid):
                    self._procs[meta["process_id"]] = ManagedProcess(meta["process_id"], None, meta)
            except Exception:
                continue

    def start(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        name: str | None = None,
        shell: bool = True,
        hidden: bool = True,
    ) -> ManagedProcess:
        process_id = f"proc_{uuid.uuid4().hex[:10]}"
        log_path = _process_log_path(process_id)
        command_text = command if isinstance(command, str) else subprocess.list2cmdline([str(c) for c in command])
        cwd_path = Path(cwd).expanduser().resolve() if cwd else PROJECT_ROOT
        log_file = open(log_path, "ab", buffering=0)
        try:
            popen = subprocess.Popen(
                command if isinstance(command, str) else [str(c) for c in command],
                cwd=str(cwd_path),
                env=sanitized_env(env),
                shell=shell,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_creation_flags(hidden=hidden),
                executable=os.environ.get("COMSPEC", "cmd.exe") if IS_WINDOWS and shell else None,
            )
        except Exception:
            log_file.close()
            raise
        finally:
            # The child keeps its own duplicate of the handle.
            try:
                log_file.close()
            except Exception:
                pass

        metadata = {
            "process_id": process_id,
            "pid": popen.pid,
            "name": name or process_id,
            "command": redact(command_text, max_chars=4000),
            "cwd": str(cwd_path),
            "log_path": str(log_path),
            "started_at": time.time(),
            "hidden": hidden,
        }
        _process_meta_path(process_id).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        managed = ManagedProcess(process_id, popen, metadata)
        with self._lock:
            self._procs[process_id] = managed
        return managed

    def get(self, process_id: str) -> ManagedProcess | None:
        with self._lock:
            proc = self._procs.get(process_id)
        if proc:
            if not proc.running() and proc.popen is not None:
                try:
                    proc.metadata["exit_code"] = proc.popen.poll()
                except Exception:
                    pass
            return proc
        # Look up by PID as a convenience.
        with self._lock:
            for p in self._procs.values():
                if str(p.pid) == str(process_id):
                    return p
        return None

    def list(self, include_all: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            managed = list(self._procs.values())
        rows = []
        for proc in managed:
            meta = dict(proc.metadata)
            meta["running"] = proc.running()
            meta["exit_code"] = proc.exit_code()
            meta["pid"] = proc.pid
            rows.append(meta)
        if include_all:
            rows.extend(_system_processes())
        return rows

    def stop(self, process_id: str, timeout: float = 10.0) -> dict[str, Any]:
        proc = self.get(process_id)
        if not proc:
            return {"ok": False, "error": f"Unknown managed process: {process_id}"}
        pid = proc.pid
        if not pid:
            return {"ok": False, "error": "Process has no PID"}
        if not proc.running():
            return {"ok": True, "process_id": proc.process_id, "pid": pid, "already_stopped": True}
        kill_process_tree(pid)
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline and proc.running():
            time.sleep(0.1)
        running = proc.running()
        return {
            "ok": not running,
            "process_id": proc.process_id,
            "pid": pid,
            "stopped": not running,
            "error": "Process did not exit after kill" if running else "",
        }

    def logs(self, process_id: str, lines: int = 100, max_chars: int = 50_000) -> dict[str, Any]:
        proc = self.get(process_id)
        if not proc:
            return {"ok": False, "error": f"Unknown managed process: {process_id}"}
        path = Path(proc.metadata.get("log_path", ""))
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_chars * 4))
                data = fh.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            data = ""
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        all_lines = data.splitlines()
        selected = "\n".join(all_lines[-max(1, int(lines)):])
        selected, truncated = truncate_output(redact(selected), max_chars)
        return {
            "ok": True,
            "process_id": proc.process_id,
            "pid": proc.pid,
            "running": proc.running(),
            "log_path": str(path),
            "lines": selected,
            "truncated": truncated,
            "exit_code": proc.exit_code(),
        }

    def wait(self, process_id: str, timeout: float = 30.0) -> dict[str, Any]:
        proc = self.get(process_id)
        if not proc:
            return {"ok": False, "error": f"Unknown managed process: {process_id}"}
        deadline = time.time() + max(0.1, timeout)
        while time.time() < deadline and proc.running():
            time.sleep(0.1)
        running = proc.running()
        return {
            "ok": not running,
            "process_id": proc.process_id,
            "pid": proc.pid,
            "running": running,
            "exit_code": proc.exit_code(),
            "timed_out": running,
        }


def _system_processes(limit: int = 40) -> list[dict[str, Any]]:
    if not IS_WINDOWS:
        try:
            out = subprocess.run(["ps", "-eo", "pid,ppid,comm,pcpu,pmem", "--sort=-pcpu"], capture_output=True, text=True, timeout=5).stdout
            lines = out.splitlines()[: limit + 1]
            return [{"source": "system", "line": line} for line in lines]
        except Exception:
            return []
    try:
        count = max(1, min(limit, 200))
        script = (
            "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First "
            + str(count)
            + " Name,Id,@{Name='RAM_MB';Expression={[math]::Round($_.WorkingSet64/1MB,1)}},"
            "@{Name='CPU_s';Expression={[math]::Round($_.CPU,1)}} | ConvertTo-Json -Compress"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_creation_flags(),
        )
        data = json.loads(proc.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        for row in data:
            row["source"] = "system"
        return data
    except Exception:
        return []


process_manager = ProcessManager()


# ---------------------------------------------------------------------------
# Resource monitoring
# ---------------------------------------------------------------------------


class ResourceMonitor:
    def __init__(self, cache_seconds: float = 5.0):
        self.cache_seconds = cache_seconds
        self._cached: dict[str, Any] = {}
        self._cached_at = 0.0
        self._lock = threading.RLock()
        self._gpu_cache: list[dict[str, Any]] = []
        self._gpu_cached_at = 0.0

    def snapshot(self, cwd: str | Path | None = None, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if not force and self._cached and (time.time() - self._cached_at) < self.cache_seconds:
                return dict(self._cached)
            data: dict[str, Any] = {
                "timestamp": time.time(),
                "platform": sys.platform,
                "hostname": os.environ.get("COMPUTERNAME") or os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", ""),
                "cpu_count": os.cpu_count() or 1,
                "python": sys.version.split()[0],
            }
            try:
                load = os.getloadavg() if hasattr(os, "getloadavg") else None
                data["load_average"] = [round(x, 2) for x in load] if load else None
            except OSError:
                data["load_average"] = None
            if IS_WINDOWS:
                data["memory"] = _windows_memory()
            else:
                data["memory"] = _posix_memory()
            disk_path = Path(cwd).expanduser() if cwd else Path.home()
            try:
                usage = shutil.disk_usage(str(disk_path))
                data["disk"] = {
                    "path": str(disk_path),
                    "total_gb": round(usage.total / (1024**3), 2),
                    "free_gb": round(usage.free / (1024**3), 2),
                    "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
                }
            except Exception:
                data["disk"] = {}
            data["gpu"] = self.gpus(force=force)
            self._cached = data
            self._cached_at = time.time()
            return dict(data)

    def gpus(self, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if not force and (time.time() - self._gpu_cached_at) < 30:
                return list(self._gpu_cache)
            result: list[dict[str, Any]] = []
            nvidia = shutil.which("nvidia-smi")
            if nvidia:
                try:
                    proc = subprocess.run(
                        [
                            nvidia,
                            "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                            "--format=csv,noheader,nounits",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        creationflags=_creation_flags(),
                    )
                    for line in proc.stdout.splitlines():
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 4:
                            result.append(
                                {
                                    "vendor": "nvidia",
                                    "name": parts[0],
                                    "memory_total_mb": _to_int(parts[1]),
                                    "memory_used_mb": _to_int(parts[2]),
                                    "utilization_percent": _to_int(parts[3]),
                                }
                            )
                except Exception:
                    pass
            self._gpu_cache = result
            self._gpu_cached_at = time.time()
            return list(result)

    def capacity(self, cwd: str | Path | None = None) -> dict[str, Any]:
        snap = self.snapshot(cwd=cwd)
        mem = snap.get("memory") or {}
        return {
            "cpu_cores": snap.get("cpu_count", 1),
            "memory_gb": round((mem.get("available_bytes") or 0) / (1024**3), 2),
            "memory_total_gb": round((mem.get("total_bytes") or 0) / (1024**3), 2),
            "disk_free_gb": (snap.get("disk") or {}).get("free_gb"),
            "gpus": snap.get("gpu", []),
            "gpu_count": len(snap.get("gpu", [])),
        }


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _windows_memory() -> dict[str, Any]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {}
        return {
            "total_bytes": int(status.ullTotalPhys),
            "available_bytes": int(status.ullAvailPhys),
            "used_percent": float(status.dwMemoryLoad),
        }
    except Exception:
        return {}


def _posix_memory() -> dict[str, Any]:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                key, value = line.split(":", 1)
                info[key] = int(value.strip().split()[0]) * 1024
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_percent": round((1 - available / total) * 100, 1) if total else None,
        }
    except Exception:
        return {}


resources = ResourceMonitor()


# ---------------------------------------------------------------------------
# Language / interpreter helpers
# ---------------------------------------------------------------------------


LANGUAGE_SPECS: dict[str, dict[str, Any]] = {
    "python": {"ext": ".py", "candidates": [sys.executable, "python", "python3"]},
    "py": {"ext": ".py", "candidates": [sys.executable, "python", "python3"]},
    "node": {"ext": ".js", "candidates": ["node"]},
    "javascript": {"ext": ".js", "candidates": ["node"]},
    "typescript": {"ext": ".ts", "candidates": ["tsx", "ts-node", "deno"]},
    "powershell": {"ext": ".ps1", "candidates": ["pwsh", "powershell"]},
    "pwsh": {"ext": ".ps1", "candidates": ["pwsh", "powershell"]},
    "bash": {"ext": ".sh", "candidates": ["bash", "sh"]},
    "sh": {"ext": ".sh", "candidates": ["sh"]},
    "cmd": {"ext": ".bat", "candidates": ["cmd"]},
    "batch": {"ext": ".bat", "candidates": ["cmd"]},
    "ruby": {"ext": ".rb", "candidates": ["ruby"]},
    "php": {"ext": ".php", "candidates": ["php"]},
    "perl": {"ext": ".pl", "candidates": ["perl"]},
    "go": {"ext": ".go", "candidates": ["go"]},
    "rust": {"ext": ".rs", "candidates": ["rustc"]},
    "java": {"ext": ".java", "candidates": ["java"]},
}


def resolve_interpreter(language: str) -> tuple[str | None, str, str]:
    """Return (executable, extension, error)."""
    lang = (language or "python").lower().strip()
    spec = LANGUAGE_SPECS.get(lang)
    if not spec:
        return None, ".txt", f"Unsupported language: {language}"
    for candidate in spec["candidates"]:
        if os.path.isabs(candidate) and Path(candidate).exists():
            return candidate, spec["ext"], ""
        found = shutil.which(candidate)
        if found:
            return found, spec["ext"], ""
    return None, spec["ext"], f"No interpreter found for {language} (tried {', '.join(spec['candidates'])})"


def interpreter_args(language: str, exe: str, script_path: Path, args: Iterable[str] = ()) -> list[str]:
    lang = language.lower().strip()
    args = [str(a) for a in args]
    if lang in ("python", "py"):
        return [exe, "-I", str(script_path), *args]
    if lang in ("powershell", "pwsh"):
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_path), *args]
    if lang in ("cmd", "batch") and IS_WINDOWS:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(script_path), *args]
    if lang in ("java",):
        return [exe, str(script_path), *args]
    if lang in ("go",):
        return [exe, "run", str(script_path), *args]
    if lang in ("rust",):
        return [exe, str(script_path), *args]
    return [exe, str(script_path), *args]


def process_info(pid: int) -> dict[str, Any]:
    """Best-effort process metadata without psutil."""
    info: dict[str, Any] = {"pid": pid, "alive": pid_alive(pid)}
    if not IS_WINDOWS:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            info["command"] = redact(cmdline)
            info["name"] = Path((Path(f"/proc/{pid}/comm")).read_text()).read_text().strip() if Path(f"/proc/{pid}/comm").exists() else ""
        except Exception:
            pass
        return info
    try:
        script = f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | Select-Object ProcessId,Name,CommandLine,WorkingSetSize | ConvertTo-Json -Compress"
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_creation_flags(),
        )
        data = json.loads(proc.stdout or "{}")
        if isinstance(data, dict):
            if data.get("CommandLine"):
                data["CommandLine"] = redact(data["CommandLine"], max_chars=4000)
            info.update(data)
    except Exception:
        pass
    return info
