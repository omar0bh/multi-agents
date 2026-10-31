"""Local PC Bridge Daemon.

Runs quietly on the user's local PC (Windows/macOS/Linux).  It polls the 24/7
cloud server, advertises capabilities, leases work, streams progress, renews
leases, honours cancellation requests, reports verified results and keeps a
local idempotency cache so a lost report never re-runs a completed task.

v3 delegates execution to :mod:`app.bridge.executor`; the original v2 actions
are preserved as ``_execute_legacy_task`` and used as a fallback for actions
that have not been upgraded yet.
"""

import argparse
import json
import os
import platform
import queue
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Force UTF-8 on Windows consoles to support Arabic paths and emojis cleanly
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

# Add project root to sys.path and load environment variables
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

import httpx

from app.bridge.audit import get_task_result
from app.bridge.executor import bridge_executor
from app.bridge.runner import cancellations
from app.tools.file_reader import delete_path, find_file, list_directory, read_text_file
from app.tools.image_downloader import download_web_image
from app.tools.project_scaffold import create_project_on_desktop, get_desktop_dir
from app.tools.terminal_tool import run_safe_command

DEFAULT_SERVER_URL = os.getenv("AWS_SERVER_URL", "http://51.20.12.138")
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "")
POLL_INTERVAL = max(1.0, float(os.getenv("BRIDGE_POLL_INTERVAL", "10")))
MAX_PARALLEL_TASKS = max(1, int(os.getenv("BRIDGE_MAX_PARALLEL_TASKS", "4")))
LEASE_SECONDS = max(30, int(os.getenv("BRIDGE_LEASE_SECONDS", "90")))
REPORT_ATTEMPTS = max(1, int(os.getenv("BRIDGE_REPORT_ATTEMPTS", "4")))
MAX_LOG_BYTES = int(os.getenv("BRIDGE_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
HEARTBEAT_TIMEOUT = 25.0
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"

_LOG_LOCK = threading.RLock()
_ACTIVE_LOCK = threading.RLock()
_ACTIVE_TASKS: dict[str, dict] = {}
_CANCEL_REQUESTS: set[str] = set()
_SHUTDOWN = threading.Event()
_CAPS_CACHE: dict = {"at": 0.0, "data": None}


def _auth_headers() -> dict:
    """Return auth headers for every request to the cloud server."""
    h = {"Content-Type": "application/json"}
    if BRIDGE_SECRET:
        h["X-Bridge-Secret"] = BRIDGE_SECRET
    return h


def gather_pc_info() -> dict:
    from app.tools.user_folders import get_known_user_folders

    try:
        desktop = get_known_user_folders().get("desktop", get_desktop_dir())
    except Exception:
        desktop = get_desktop_dir()
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "desktop_dir": str(desktop),
        "user": os.getenv("USERNAME") or os.getenv("USER") or "user",
        "timestamp": time.time(),
        "worker": WORKER_ID,
    }


def safe_print(msg: str) -> None:
    """Thread-safe console + rotating file log."""
    with _LOG_LOCK:
        log_file = PROJECT_ROOT / "bridge.log"
        try:
            if log_file.exists() and log_file.stat().st_size > MAX_LOG_BYTES:
                rotated = PROJECT_ROOT / "bridge.log.1"
                try:
                    if rotated.exists():
                        rotated.unlink()
                    log_file.replace(rotated)
                except OSError:
                    pass
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass

        if sys.stdout is not None:
            try:
                text = str(msg) + "\n"
                sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
                sys.stdout.buffer.flush()
            except Exception:
                try:
                    print(str(msg), flush=True)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Legacy v2 actions (fallback for actions not yet in the new registry)
# ---------------------------------------------------------------------------


def _execute_legacy_task(action: str, params: dict) -> dict:
    """Execute a single legacy v2 task locally. Wrapped with error handling."""
    safe_print(f"[*] Executing legacy local task: {action} | params: {params}")
    try:
        if action == "create_project":
            name = params.get("name", "project")
            target_location = params.get("target_location") or params.get("folder")
            return create_project_on_desktop(name, target_location=target_location)

        elif action == "run_command":
            cmd = params.get("cmd", "")
            return run_safe_command(cmd)

        elif action in ("delete_path", "delete"):
            path = (params.get("path") or params.get("src") or params.get("target") or params.get("folder") or "").strip()
            return delete_path(path)

        elif action == "download_image":
            query = params.get("query", "")
            folder = params.get("folder", "pictures")
            filename = params.get("filename", "")
            return download_web_image(query, folder_name=folder, filename=filename)

        elif action in ("find_file", "read_file"):
            query = (params.get("query") or params.get("src") or params.get("path") or "").strip()
            found = find_file(query)
            if not found:
                from app.tools.user_folders import get_known_user_folders

                desktop = get_known_user_folders()["desktop"]
                if (desktop / query).exists():
                    found = desktop / query
            if found and found.is_file():
                preview = read_text_file(str(found), max_chars=2000)
                return {
                    "ok": True,
                    "action": "read_file",
                    "path": str(found),
                    "preview": preview,
                    "detail": f"📄 **محتوى الملف `{found.name}`:**\n```\n{preview}\n```",
                }
            return {"ok": False, "error": f"تعذر العثور على الملف `{query}` على حاسوبك الشخصي."}

        elif action in ("list_dir", "ls"):
            from app.tools.user_folders import get_known_user_folders

            folder = (params.get("folder") or params.get("src") or "").strip()
            parent = (params.get("parent") or params.get("dest_folder") or "").strip().lower()
            known = get_known_user_folders()

            target_path = None
            if folder:
                p = Path(folder).expanduser()
                if p.is_dir():
                    target_path = p
                elif folder.lower() in known:
                    target_path = known[folder.lower()]
                else:
                    bases = []
                    if parent and parent in known:
                        bases.append(known[parent])
                    bases.extend(
                        [
                            known["desktop"],
                            known["documents"],
                            known["downloads"],
                            known["pictures"],
                            known["videos"],
                            known["music"],
                            Path.home(),
                        ]
                    )
                    for base in bases:
                        cand = base / folder
                        if cand.is_dir():
                            target_path = cand
                            break
            elif parent and parent in known:
                target_path = known[parent]
            else:
                target_path = known["desktop"]

            if target_path and target_path.is_dir():
                items = list_directory(str(target_path))
                return {
                    "ok": True,
                    "path": str(target_path),
                    "folder_name": target_path.name,
                    "items": items,
                    "count": len(items),
                    "detail": "\n".join(items) if items else "المجلد فارغ.",
                }
            return {
                "ok": False,
                "error": f"المجلد `{folder or parent}` غير موجود على حاسوبك الشخصي.",
                "desktop": str(known["desktop"]),
            }

        elif action in ("copy_file", "copy"):
            import shutil

            from app.tools.user_folders import get_known_user_folders

            known = get_known_user_folders()
            desktop = known["desktop"]

            src = (params.get("src") or "").strip().strip("'\"`")
            src_parent = (params.get("src_parent") or "").strip().strip("'\"`")
            dest_folder = (params.get("dest_folder") or "desktop").strip().strip("'\"`")
            dest_name = (params.get("dest_name") or "").strip().strip("'\"`")

            src_path = None
            if src:
                if src_parent:
                    cand_parent = desktop / src_parent
                    if (cand_parent / src).exists():
                        src_path = (cand_parent / src).resolve()

                if not src_path:
                    direct = Path(src).expanduser()
                    if direct.exists():
                        src_path = direct.resolve()
                    elif (desktop / src).exists():
                        src_path = (desktop / src).resolve()

                if not src_path:
                    for k_name, k_dir in known.items():
                        if (k_dir / src).exists():
                            src_path = (k_dir / src).resolve()
                            break

                if not src_path:
                    target_name = Path(src).name.lower()
                    for root, dirs, files in os.walk(desktop):
                        rel = Path(root).relative_to(desktop)
                        if len(rel.parts) > 3:
                            dirs.clear()
                            continue
                        for d in dirs:
                            if d.lower() == target_name:
                                src_path = (Path(root) / d).resolve()
                                break
                        if src_path:
                            break
                        for f in files:
                            if f.lower() == target_name:
                                src_path = (Path(root) / f).resolve()
                                break
                        if src_path:
                            break

            if not src_path or not src_path.exists():
                return {
                    "ok": False,
                    "error": f"تعذر العثور على الملف أو المجلد `{src}` على حاسوبك الشخصي (تم البحث في Desktop والمجلدات التابعة).",
                    "desktop": str(desktop),
                }

            dest_dir = None
            if dest_folder.lower() in known:
                dest_dir = known[dest_folder.lower()]
            else:
                direct_dir = Path(dest_folder).expanduser()
                if direct_dir.is_dir():
                    dest_dir = direct_dir.resolve()
                else:
                    dest_dir = (desktop / dest_folder).resolve()
                    dest_dir.mkdir(parents=True, exist_ok=True)

            target_name = dest_name if dest_name else src_path.name
            final_dest = dest_dir / target_name

            if final_dest.resolve() == src_path.resolve():
                return {
                    "ok": False,
                    "error": f"المسار المصدر والهدف متطابقان (`{src_path}`). لا يمكن النسخ إلى نفس المكان بنفس الاسم.",
                }

            if src_path.is_dir():
                shutil.copytree(str(src_path), str(final_dest), dirs_exist_ok=True)
                is_dir_text = "المجلد"
            else:
                shutil.copy2(str(src_path), str(final_dest))
                is_dir_text = "الملف"

            return {
                "ok": True,
                "action": "copy_file",
                "src": str(src_path),
                "dest": str(final_dest),
                "is_folder": src_path.is_dir(),
                "detail": f"تم نسخ {is_dir_text} `{src_path.name}` بنجاح إلى:\n`{final_dest}`",
            }

        elif action in ("create_folder", "mkdir"):
            from app.tools.user_folders import get_known_user_folders

            known = get_known_user_folders()
            desktop = known["desktop"]
            folder = (params.get("folder") or params.get("src") or params.get("name") or "NewFolder").strip().strip("'\"`")
            parent = (params.get("parent") or params.get("dest_folder") or "desktop").strip()

            base = known.get(parent.lower())
            if not base:
                cand = desktop / parent
                if cand.is_dir():
                    base = cand
                else:
                    base = desktop

            target_dir = base / folder
            target_dir.mkdir(parents=True, exist_ok=True)
            return {
                "ok": True,
                "action": "create_folder",
                "path": str(target_dir),
                "folder_name": target_dir.name,
                "detail": f"تم إنشاء المجلد `{target_dir.name}` بنجاح في:\n`{target_dir}`",
            }

        elif action in ("move_file", "rename"):
            import shutil

            from app.tools.user_folders import get_known_user_folders

            known = get_known_user_folders()
            desktop = known["desktop"]

            src = (params.get("src") or params.get("path") or "").strip().strip("'\"`")
            dest = (params.get("dest") or params.get("dest_folder") or params.get("dest_name") or "").strip().strip("'\"`")
            src_parent = (params.get("src_parent") or "").strip().strip("'\"`")

            src_path = None
            if src:
                if src_parent:
                    cand = desktop / src_parent / src
                    if cand.exists():
                        src_path = cand
                if not src_path:
                    direct = Path(src).expanduser()
                    if direct.exists():
                        src_path = direct
                    elif (desktop / src).exists():
                        src_path = desktop / src
                    else:
                        found = find_file(src)
                        if found:
                            src_path = found

            if not src_path:
                return {
                    "ok": False,
                    "error": f"الملف أو المجلد `{src}` غير موجود على حاسوبك الشخصي لنقله أو تعديل اسمه.",
                }

            if not dest:
                return {"ok": False, "error": "لم يتم تحديد الوجهة أو الاسم الجديد."}

            if "/" not in dest and "\\" not in dest and "." in dest and src_path.is_file():
                dest_path = src_path.parent / dest
            elif dest.lower() in known:
                dest_path = known[dest.lower()] / src_path.name
            else:
                direct_dest = Path(dest).expanduser()
                if direct_dest.is_dir():
                    dest_path = direct_dest / src_path.name
                elif (desktop / dest).is_dir():
                    dest_path = (desktop / dest) / src_path.name
                else:
                    dest_path = src_path.parent / dest

            shutil.move(str(src_path), str(dest_path))
            return {
                "ok": True,
                "action": "move_file",
                "src": str(src_path),
                "dest": str(dest_path),
                "detail": f"تم نقل/تعديل اسم `{src_path.name}` بنجاح إلى:\n`{dest_path}`",
            }

        elif action in ("create_file", "write_file"):
            from app.tools.user_folders import get_known_user_folders

            known = get_known_user_folders()
            desktop = known["desktop"]
            filename = (params.get("filename") or params.get("src") or "new_file.txt").strip().strip("'\"`")
            folder = (params.get("folder") or params.get("dest_folder") or "desktop").strip()
            content = params.get("content", "")

            base = known.get(folder.lower())
            if not base:
                cand = desktop / folder
                if cand.is_dir():
                    base = cand
                else:
                    base = desktop

            file_path = base / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return {
                "ok": True,
                "action": "create_file",
                "path": str(file_path),
                "detail": f"تم إنشاء الملف `{filename}` بنجاح في:\n`{file_path}`",
            }

        # ── Desktop Vision & Layout ──────────────────────────────────────────
        elif action in ("screenshot_analyze", "desktop_analyze", "chof_desktop"):
            from app.tools.desktop_manager import cmd_screenshot_and_analyze

            result = cmd_screenshot_and_analyze()
            cats = result.get("categories", {})
            summary_lines = [f"- **{cat}** ({len(names)}): {', '.join(names[:5])}" for cat, names in cats.items()]
            result["detail"] = (
                f"📸 **تحليل الديسكتوب:**\n{result.get('description','')}\n\n"
                + "\n".join(summary_lines)
                + f"\n\n📁 الصورة محفوظة: `{result.get('screenshot_path','')}`"
            )
            result["ok"] = "error" not in result
            return result

        elif action in ("get_icons", "list_icons", "desktop_icons"):
            from app.tools.desktop_manager import cmd_get_icon_list

            result = cmd_get_icon_list()
            lines = [f"  [{i['index']}] {i['name']}  ({i['x']},{i['y']})" for i in result["icons"]]
            result["detail"] = f"🖥️ **أيقونات الديسكتوب ({result['count']}):**\n" + "\n".join(lines)
            result["ok"] = True
            return result

        elif action in ("arrange_desktop", "arrange_icons", "rattab_desktop"):
            from app.tools.desktop_manager import cmd_arrange_by_category

            rules = params.get("rules") or {}
            result = cmd_arrange_by_category(rules=rules)
            moved = result.get("moved", [])
            not_found = result.get("not_found", [])
            lines = [f"  ✅ {m['name']} → {m['zone']}" for m in moved]
            if not_found:
                lines.append(f"  ❌ مش لقيتهم: {', '.join(not_found)}")
            result["detail"] = f"🗂️ **تم ترتيب الديسكتوب!**\n" f"{result.get('analysis','')}\n\n" + "\n".join(lines)
            result["ok"] = True
            return result

        elif action in ("system_cleanup_and_report", "pc_performance_cleanup", "performance_report", "clean_pc"):
            from app.tools.system_cleaner import execute_full_system_cleanup_and_report

            return execute_full_system_cleanup_and_report()

        elif action in ("system_cleanup_and_report", "pc_performance_cleanup", "performance_report", "clean_pc"):
            from app.tools.system_cleaner import execute_full_system_cleanup_and_report
            result = execute_full_system_cleanup_and_report()
            return result

        else:
            return {"ok": False, "error": f"Unknown action: {action}"}

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# Legacy handler is used only when the action registry has no matching action.
bridge_executor.set_legacy_handler(_execute_legacy_task)


def execute_local_task(
    action: str,
    params: dict,
    *,
    task_id: str | None = None,
    progress_callback=None,
) -> dict:
    """Execute a task through the upgraded bridge executor.

    The signature stays compatible with v2 callers (``action, params``).  The
    executor resolves upgraded actions first and falls back to
    ``_execute_legacy_task`` for legacy-only actions such as ``create_project``.
    """
    return bridge_executor.execute(
        action,
        params,
        task_id=task_id,
        progress_callback=progress_callback,
    )


# ---------------------------------------------------------------------------
# Progress reporting and task lifecycle
# ---------------------------------------------------------------------------


class ProgressReporter:
    """Non-blocking progress queue so actions never wait on the network."""

    def __init__(self, server_url: str):
        self.server_url = server_url.rstrip("/")
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bridge-progress", daemon=True)
        self._thread.start()

    def submit(self, task_id: str, progress: dict) -> None:
        try:
            self._queue.put_nowait((task_id, progress))
        except queue.Full:
            pass

    def _run(self) -> None:
        client = httpx.Client(trust_env=False, timeout=8.0)
        try:
            while not self._stop.is_set():
                try:
                    task_id, progress = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    client.post(
                        f"{self.server_url}/api/bridge/task/progress",
                        json={"task_id": task_id, "progress": progress, "worker": WORKER_ID},
                        headers=_auth_headers(),
                    )
                except Exception:
                    pass
        finally:
            client.close()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._thread.join(timeout=2)
        except Exception:
            pass


def _mark_active(task_id: str, action: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_TASKS[task_id] = {"task_id": task_id, "action": action, "started_at": time.time()}


def _unmark_active(task_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_TASKS.pop(task_id, None)
        _CANCEL_REQUESTS.discard(task_id)


def _is_active(task_id: str) -> bool:
    with _ACTIVE_LOCK:
        return task_id in _ACTIVE_TASKS


def _active_ids() -> list[str]:
    with _ACTIVE_LOCK:
        return sorted(_ACTIVE_TASKS)


def _note_cancel(task_id: str) -> None:
    with _ACTIVE_LOCK:
        _CANCEL_REQUESTS.add(task_id)


def _cancel_requested(task_id: str) -> bool:
    with _ACTIVE_LOCK:
        return task_id in _CANCEL_REQUESTS


def _capabilities_payload(force: bool = False) -> dict:
    """Cache the capability manifest for five minutes (it is sent with heartbeats)."""
    now = time.time()
    if not force and _CAPS_CACHE["data"] and now - _CAPS_CACHE["at"] < 300:
        return _CAPS_CACHE["data"]
    try:
        from app.bridge.capabilities import bridge_capabilities

        caps = bridge_capabilities(include_actions=True, include_resources=True)
        actions = caps.get("actions") or []
        caps["actions"] = [a.get("name") for a in actions if isinstance(a, dict)]
        caps["action_count"] = len(caps["actions"]) + len(caps.get("legacy_actions") or [])
    except Exception as exc:
        caps = {"error": f"{type(exc).__name__}: {exc}", "features": []}
    _CAPS_CACHE.update({"at": now, "data": caps})
    return caps


def _post_with_retries(client: httpx.Client, url: str, payload: dict) -> httpx.Response | None:
    last_error: Exception | None = None
    for attempt in range(REPORT_ATTEMPTS):
        if _SHUTDOWN.is_set():
            break
        try:
            response = client.post(url, json=payload, headers=_auth_headers())
            if response.status_code < 500:
                return response
            last_error = RuntimeError(f"server returned {response.status_code}")
        except Exception as exc:
            last_error = exc
        if attempt + 1 < REPORT_ATTEMPTS:
            _SHUTDOWN.wait(min(2 ** attempt, 10))
    if last_error:
        safe_print(f"[!] Report to {url} failed after {REPORT_ATTEMPTS} attempt(s): {last_error}")
    return None


def _send_telegram_artifacts(action: str, exec_result: dict, chat_id) -> None:
    """Send screenshots / cleanup reports directly to Telegram when requested."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token or not chat_id:
        return
    if action in ("screenshot_analyze", "desktop_analyze", "chof_desktop"):
        img_path = exec_result.get("screenshot_path")
        if img_path and os.path.exists(img_path):
            try:
                with open(img_path, "rb") as photo_file:
                    httpx.post(
                        f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                        data={"chat_id": chat_id, "caption": "📸 **صورة شاشة حاسوبك الشخصي (Desktop Screenshot)**"},
                        files={"photo": photo_file},
                        timeout=30.0,
                    )
            except Exception as photo_err:
                safe_print(f"[!] Failed to upload screenshot to Telegram: {photo_err}")

    if action in ("system_cleanup_and_report", "pc_performance_cleanup", "performance_report", "clean_pc"):
        for f_key, caption in [
            ("txt_path", "📄 تقرير أداء وتنظيف الحاسوب الشامل (Performance Report)"),
            ("csv_path", "📊 قائمة العمليات الأكثر استهلاكاً للموارد (Processes CSV)"),
        ]:
            doc_path = exec_result.get(f_key)
            if doc_path and os.path.exists(doc_path):
                try:
                    with open(doc_path, "rb") as doc_file:
                        httpx.post(
                            f"https://api.telegram.org/bot{bot_token}/sendDocument",
                            data={"chat_id": chat_id, "caption": caption},
                            files={"document": doc_file},
                            timeout=30.0,
                        )
                except Exception as doc_err:
                    safe_print(f"[!] Failed to upload {f_key} to Telegram: {doc_err}")

        # If system cleanup & performance report was generated, send both files directly to Telegram
        if action in ("system_cleanup_and_report", "pc_performance_cleanup", "performance_report", "clean_pc") and chat_id:
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            if bot_token:
                for f_key, caption in [
                    ("txt_path", "📄 تقرير أداء وتنظيف الحاسوب الشامل (Performance Report)"),
                    ("csv_path", "📊 قائمة العمليات الأكثر استهلاكاً للموارد (Processes CSV)"),
                ]:
                    doc_path = exec_result.get(f_key)
                    if doc_path and os.path.exists(doc_path):
                        try:
                            with open(doc_path, "rb") as doc_file:
                                httpx.post(
                                    f"https://api.telegram.org/bot{bot_token}/sendDocument",
                                    data={"chat_id": chat_id, "caption": caption},
                                    files={"document": doc_file},
                                    timeout=30.0,
                                )
                        except Exception as doc_err:
                            safe_print(f"[!] Failed to upload {f_key} to Telegram: {doc_err}")


def _report_result(
    client: httpx.Client,
    report_url: str,
    task_id: str,
    action: str,
    exec_result: dict,
    chat_id,
) -> None:
    response = _post_with_retries(
        client,
        report_url,
        {
            "task_id": task_id,
            "result": exec_result,
            "chat_id": chat_id,
            "worker": WORKER_ID,
        },
    )
    if response is not None:
        status = "✅" if exec_result.get("ok") else "❌"
        safe_print(f"[{status}] Task {task_id} ({action}) reported to server ({response.status_code}).")


def _process_task_and_report(
    task: dict,
    report_url: str,
    client: httpx.Client,
    reporter: ProgressReporter,
) -> None:
    """Execute one leased task and report its result."""
    task_id = str(task.get("task_id") or "")
    action = str(task.get("action") or "")
    params = task.get("params") or {}
    chat_id = task.get("chat_id")
    if not task_id:
        return

    try:
        if _cancel_requested(task_id):
            cancel_result = {
                "ok": False,
                "action": action,
                "status": "cancelled",
                "cancelled": True,
                "error": "Cancelled before execution started",
            }
            _report_result(client, report_url, task_id, action, cancel_result, chat_id)
            return

        cached = get_task_result(task_id)
        if cached:
            cached = dict(cached)
            cached["action"] = action
            cached["cached_result"] = True
            safe_print(f"[*] Task {task_id} already has a cached result; reporting it without re-running.")
            _report_result(client, report_url, task_id, action, cached, chat_id)
            return

        _mark_active(task_id, action)
        safe_print(f"[*] Executing task {task_id}: {action}")
        exec_result = execute_local_task(
            action,
            params,
            task_id=task_id,
            progress_callback=lambda progress: reporter.submit(task_id, progress),
        )
        if not isinstance(exec_result, dict):
            exec_result = {"ok": False, "error": "Action returned a non-object result"}
        exec_result["canonical_action"] = exec_result.get("action")
        exec_result["action"] = action
        exec_result.setdefault("task_id", task_id)

        _send_telegram_artifacts(action, exec_result, chat_id)
        _report_result(client, report_url, task_id, action, exec_result, chat_id)
    except Exception as exc:
        safe_print(f"[!] Task {task_id} crashed: {exc}")
        error_result = {"ok": False, "action": action, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        try:
            _report_result(client, report_url, task_id, action, error_result, chat_id)
        except Exception:
            pass
    finally:
        _unmark_active(task_id)


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------


def run_bridge_loop(server_url: str):
    clean_url = server_url.rstrip("/")
    heartbeat_url = f"{clean_url}/api/bridge/heartbeat"
    report_url = f"{clean_url}/api/bridge/task/report"

    safe_print("=" * 64)
    safe_print("[BRIDGE] DSPy Cloud-to-Local PC Bridge v3 Active")
    safe_print(f"[*] Target Server  : {clean_url}")
    safe_print(f"[*] Local Machine  : {socket.gethostname()} ({platform.system()})")
    safe_print(f"[*] Desktop Path   : {get_desktop_dir()}")
    safe_print(f"[*] Worker ID      : {WORKER_ID}")
    safe_print(f"[*] Auth           : {'enabled' if BRIDGE_SECRET else 'disabled (no BRIDGE_SECRET set)'}")
    safe_print(f"[*] Parallel Tasks : up to {MAX_PARALLEL_TASKS} at a time")
    safe_print(f"[*] Lease          : {LEASE_SECONDS}s (renewed every heartbeat)")
    safe_print("=" * 64)
    safe_print("[+] Bridge running... Press Ctrl+C to stop.")

    client = httpx.Client(trust_env=False, timeout=HEARTBEAT_TIMEOUT)
    pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL_TASKS, thread_name_prefix="bridge-task")
    reporter = ProgressReporter(clean_url)
    delay = POLL_INTERVAL
    capabilities = _capabilities_payload()

    try:
        while not _SHUTDOWN.is_set():
            heartbeat_ok = False
            try:
                info = gather_pc_info()
                info.update(
                    {
                        "worker": WORKER_ID,
                        "protocol_version": capabilities.get("protocol_version"),
                        "capabilities": capabilities,
                        "renew_task_ids": _active_ids(),
                        "active_tasks": [
                            {"task_id": tid, "action": _ACTIVE_TASKS[tid].get("action")}
                            for tid in _active_ids()
                            if tid in _ACTIVE_TASKS
                        ],
                        "lease_seconds": LEASE_SECONDS,
                        "limit": MAX_PARALLEL_TASKS,
                        "claim_tasks": True,
                    }
                )
                response = client.post(heartbeat_url, json=info, headers=_auth_headers())

                if response.status_code == 200:
                    heartbeat_ok = True
                    data = response.json()
                    safe_print(f"[+] Heartbeat sent ({time.strftime('%H:%M:%S')}) - PC is ONLINE on AWS")

                    for cancel_id in data.get("cancel_task_ids") or []:
                        cancel_id = str(cancel_id)
                        _note_cancel(cancel_id)
                        if cancellations.cancel(cancel_id, "cancelled by user"):
                            safe_print(f"[*] Cancellation signalled for task {cancel_id}")

                    pending_tasks = data.get("pending_tasks") or []
                    if pending_tasks:
                        safe_print(f"[*] Received {len(pending_tasks)} task(s) — scheduling...")
                        for task in pending_tasks:
                            task_id = str(task.get("task_id") or "")
                            if not task_id:
                                continue
                            if _is_active(task_id):
                                safe_print(f"[*] Task {task_id} is already running locally; skipping duplicate dispatch.")
                                continue
                            pool.submit(_process_task_and_report, task, report_url, client, reporter)
                elif response.status_code == 401:
                    safe_print("[!] AUTH REJECTED: X-Bridge-Secret is wrong. Check BRIDGE_SECRET in .env")
                else:
                    safe_print(f"[!] Server returned status {response.status_code}: {response.text[:300]}")

            except httpx.ConnectError:
                safe_print("[!] Waiting for server connection...")
            except httpx.ReadTimeout:
                safe_print("[!] Heartbeat timed out; retrying with backoff...")
            except Exception as exc:
                safe_print(f"[!] Bridge Error: {type(exc).__name__}: {exc}")

            if heartbeat_ok:
                delay = POLL_INTERVAL
            else:
                delay = min(max(delay * 1.5, POLL_INTERVAL), 60.0)
            _SHUTDOWN.wait(delay)
    finally:
        safe_print("[*] Shutting down bridge (cancelling running tasks)...")
        _SHUTDOWN.set()
        cancellations.cancel_all("bridge shutting down")
        reporter.stop()
        pool.shutdown(wait=False, cancel_futures=True)
        try:
            client.close()
        except Exception:
            pass


def _install_signal_handlers() -> None:
    """Allow graceful shutdown when the service manager asks us to stop."""
    try:
        import signal

        def _handler(signum, frame):  # noqa: ARG001
            _SHUTDOWN.set()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handler)
                except (ValueError, OSError):
                    pass
    except Exception:
        pass


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description="DSPy Cloud-to-Local Bridge v3")
        parser.add_argument("--server", default=DEFAULT_SERVER_URL, help="AWS Server base URL")
        args = parser.parse_args()
        _install_signal_handlers()
        run_bridge_loop(args.server)
    except KeyboardInterrupt:
        safe_print("\n[!] Bridge stopped by user.")
    except Exception as e:
        import traceback

        crash_log = PROJECT_ROOT / "bridge_crash.log"
        with open(crash_log, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        safe_print(f"[!] Bridge crashed. See: {crash_log}")
