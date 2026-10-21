"""Local PC Bridge Daemon.
Runs quietly on the user's local PC (Windows/Mac/Linux).
Periodically pings the 24/7 AWS Cloud Server, reports local PC online status,
and executes queued tasks (e.g. creating projects on Desktop, running builds, managing files)
even when the user interacts via Telegram from their phone.

Improvements over v1:
- Sends X-Bridge-Secret header for server authentication
- Executes multiple tasks in parallel using ThreadPoolExecutor
- Retry logic: failed tasks are reported with error detail
"""

import argparse
import os
import platform
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from app.tools.file_reader import delete_path, find_file, list_directory, read_text_file
from app.tools.image_downloader import download_web_image
from app.tools.project_scaffold import create_project_on_desktop, get_desktop_dir
from app.tools.terminal_tool import run_safe_command

DEFAULT_SERVER_URL = os.getenv("AWS_SERVER_URL", "http://51.20.12.138")
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "")
POLL_INTERVAL = 10  # Seconds between heartbeats
MAX_PARALLEL_TASKS = 4  # Max tasks to run at the same time


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
    }


def safe_print(msg: str):
    try:
        log_file = PROJECT_ROOT / "bridge.log"
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


def execute_local_task(action: str, params: dict) -> dict:
    """Execute a single task locally on this PC. Wrapped with error handling."""
    safe_print(f"[*] Executing local task: {action} | params: {params}")
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
                    bases.extend([
                        known["desktop"],
                        known["documents"],
                        known["downloads"],
                        known["pictures"],
                        known["videos"],
                        known["music"],
                        Path.home(),
                    ])
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

            # 1. Resolve source file or folder
            src_path = None
            if src:
                # 1a. Check relative to src_parent inside desktop (e.g. desktop / 'doc sami' / 'new')
                if src_parent:
                    cand_parent = desktop / src_parent
                    if (cand_parent / src).exists():
                        src_path = (cand_parent / src).resolve()

                # 1b. Check direct path or path with subdirectories (e.g. 'doc sami/new')
                if not src_path:
                    direct = Path(src).expanduser()
                    if direct.exists():
                        src_path = direct.resolve()
                    elif (desktop / src).exists():
                        src_path = (desktop / src).resolve()

                # 1c. Search in known user folders
                if not src_path:
                    for k_name, k_dir in known.items():
                        if (k_dir / src).exists():
                            src_path = (k_dir / src).resolve()
                            break

                # 1d. Search recursively inside Desktop (up to 3 levels deep)
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

            # 2. Resolve destination folder
            dest_dir = None
            if dest_folder.lower() in known:
                dest_dir = known[dest_folder.lower()]
            else:
                direct_dir = Path(dest_folder).expanduser()
                if direct_dir.is_dir():
                    dest_dir = direct_dir.resolve()
                else:
                    # Check or create as subfolder on Desktop (e.g. desktop / 'chess')
                    dest_dir = (desktop / dest_folder).resolve()
                    dest_dir.mkdir(parents=True, exist_ok=True)

            target_name = dest_name if dest_name else src_path.name
            final_dest = dest_dir / target_name

            # Check if source and destination are the exact same path
            if final_dest.resolve() == src_path.resolve():
                return {
                    "ok": False,
                    "error": f"المسار المصدر والهدف متطابقان (`{src_path}`). لا يمكن النسخ إلى نفس المكان بنفس الاسم.",
                }

            # 3. Perform Copy (folder vs file)
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

        else:
            return {"ok": False, "error": f"Unknown action: {action}"}

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _process_task_and_report(task: dict, report_url: str, client: httpx.Client) -> None:
    """Execute one task and immediately report result back to cloud server."""
    task_id = task.get("task_id")
    action = task.get("action")
    params = task.get("params", {})
    chat_id = task.get("chat_id")

    exec_result = execute_local_task(action, params)
    exec_result["action"] = action  # include action in result for notification

    try:
        client.post(
            report_url,
            json={
                "task_id": task_id,
                "result": exec_result,
                "chat_id": chat_id,
            },
            headers=_auth_headers(),
        )
        status = "✅" if exec_result.get("ok") else "❌"
        safe_print(f"[{status}] Task {task_id} ({action}) reported to server.")
    except Exception as e:
        safe_print(f"[!] Failed to report task {task_id}: {e}")


def run_bridge_loop(server_url: str):
    clean_url = server_url.rstrip("/")
    heartbeat_url = f"{clean_url}/api/bridge/heartbeat"
    report_url = f"{clean_url}/api/bridge/task/report"

    safe_print("=" * 60)
    safe_print("[BRIDGE] DSPy Cloud-to-Local PC Bridge v2 Active")
    safe_print(f"[*] Target Server  : {clean_url}")
    safe_print(f"[*] Local Machine  : {socket.gethostname()} ({platform.system()})")
    safe_print(f"[*] Desktop Path   : {get_desktop_dir()}")
    safe_print(f"[*] Auth           : {'enabled' if BRIDGE_SECRET else 'disabled (no BRIDGE_SECRET set)'}")
    safe_print(f"[*] Parallel Tasks : up to {MAX_PARALLEL_TASKS} at a time")
    safe_print("=" * 60)
    safe_print("[+] Bridge running... Press Ctrl+C to stop.")

    client = httpx.Client(trust_env=False, timeout=15.0)
    executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL_TASKS)

    while True:
        try:
            info = gather_pc_info()
            resp = client.post(heartbeat_url, json=info, headers=_auth_headers())

            if resp.status_code == 200:
                safe_print(f"[+] Heartbeat sent ({time.strftime('%H:%M:%S')}) - PC is ONLINE on AWS")
                data = resp.json()
                pending_tasks = data.get("pending_tasks", [])

                if pending_tasks:
                    safe_print(f"[*] Received {len(pending_tasks)} task(s) — running in parallel...")
                    # Submit all tasks to thread pool and don't block
                    for task in pending_tasks:
                        executor.submit(_process_task_and_report, task, report_url, client)

            elif resp.status_code == 401:
                safe_print("[!] AUTH REJECTED: X-Bridge-Secret is wrong. Check BRIDGE_SECRET in .env")
            else:
                safe_print(f"[!] Server returned status {resp.status_code}")

        except httpx.ConnectError:
            safe_print("[!] Waiting for server connection...")
        except Exception as e:
            safe_print(f"[!] Bridge Error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description="DSPy Cloud-to-Local Bridge v2")
        parser.add_argument("--server", default=DEFAULT_SERVER_URL, help="AWS Server base URL")
        args = parser.parse_args()
        run_bridge_loop(args.server)
    except KeyboardInterrupt:
        safe_print("\n[!] Bridge stopped by user.")
    except Exception as e:
        import traceback
        crash_log = PROJECT_ROOT / "bridge_crash.log"
        with open(crash_log, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        safe_print(f"[!] Bridge crashed. See: {crash_log}")
