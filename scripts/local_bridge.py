"""Local PC Bridge Daemon.
Runs quietly on the user's local PC (Windows/Mac/Linux).
Periodically pings the 24/7 AWS Cloud Server, reports local PC online status,
and executes queued tasks (e.g. creating projects on Desktop, running builds, managing files)
even when the user interacts via Telegram from their phone.
"""

import argparse
import os
import platform
import socket
import sys
import time
from pathlib import Path

# Force UTF-8 on Windows consoles to support Arabic paths and emojis cleanly
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

# Add project root to sys.path and load environment variables
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

import httpx

from app.tools.file_reader import delete_path, find_file, read_text_file
from app.tools.image_downloader import download_web_image
from app.tools.project_scaffold import create_project_on_desktop, get_desktop_dir
from app.tools.terminal_tool import run_safe_command

DEFAULT_SERVER_URL = os.getenv("AWS_SERVER_URL", "http://51.20.12.138")
POLL_INTERVAL = 10  # Seconds between heartbeats


def gather_pc_info() -> dict:
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
        text = str(msg) + "\n"
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        try:
            clean = str(msg).encode("ascii", errors="replace").decode("ascii")
            print(clean, flush=True)
        except Exception:
            pass


def execute_local_task(action: str, params: dict) -> dict:
    """Execute a task locally on this PC."""
    safe_print(f"[*] Executing local task: {action} with params: {params}")
    try:
        if action == "create_project":
            name = params.get("name", "project")
            target_location = params.get("target_location") or params.get("folder")
            return create_project_on_desktop(name, target_location=target_location)

        elif action == "run_command":
            cmd = params.get("cmd", "")
            return run_safe_command(cmd)

        elif action == "delete_path":
            path = params.get("path", "")
            return delete_path(path)

        elif action == "download_image":
            query = params.get("query", "")
            folder = params.get("folder", "videos")
            filename = params.get("filename", "")
            return download_web_image(query, folder_name=folder, filename=filename)

        elif action == "find_file":
            query = params.get("query", "")
            found = find_file(query)
            if found:
                preview = read_text_file(str(found), max_chars=1000)
                return {"ok": True, "path": str(found), "preview": preview}
            return {"ok": False, "error": f"File not found: {query}"}

        else:
            return {"ok": False, "error": f"Unknown action: {action}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_bridge_loop(server_url: str):
    clean_url = server_url.rstrip("/")
    heartbeat_url = f"{clean_url}/api/bridge/heartbeat"
    report_url = f"{clean_url}/api/bridge/task/report"

    safe_print("=" * 60)
    safe_print("[BRIDGE] DSPy Cloud-to-Local PC Bridge Active")
    safe_print(f"[*] Target Server: {clean_url}")
    safe_print(f"[*] Local Machine: {socket.gethostname()} ({platform.system()})")
    safe_print(f"[*] Desktop Path : {get_desktop_dir()}")
    safe_print("=" * 60)
    safe_print("[+] Bridge running... Press Ctrl+C to stop.")

    client = httpx.Client(trust_env=False, timeout=10.0)

    while True:
        try:
            info = gather_pc_info()
            resp = client.post(heartbeat_url, json=info)
            if resp.status_code == 200:
                safe_print(f"[+] Heartbeat sent ({time.strftime('%H:%M:%S')}) - PC is ONLINE on AWS")
                data = resp.json()
                pending_tasks = data.get("pending_tasks", [])
                if pending_tasks:
                    safe_print(f"[*] Received {len(pending_tasks)} tasks from cloud server!")
                    for task in pending_tasks:
                        task_id = task.get("task_id")
                        action = task.get("action")
                        params = task.get("params", {})
                        chat_id = task.get("chat_id")

                        exec_result = execute_local_task(action, params)

                        # Report completion back to cloud
                        client.post(
                            report_url,
                            json={
                                "task_id": task_id,
                                "result": exec_result,
                                "chat_id": chat_id,
                            },
                        )
                        safe_print(f"[+] Task {task_id} reported to server.")
            else:
                safe_print(f"[!] Server returned status {resp.status_code}")
        except httpx.ConnectError:
            safe_print("[!] Waiting for server connection...")
        except Exception as e:
            safe_print(f"[!] Bridge Error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description="DSPy Cloud-to-Local Bridge")
        parser.add_argument("--server", default=DEFAULT_SERVER_URL, help="AWS Server base URL")
        args = parser.parse_args()

        run_bridge_loop(args.server)
    except Exception as e:
        import traceback
        with open("C:\\dspy-multi-agents\\bridge_crash.log", "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
