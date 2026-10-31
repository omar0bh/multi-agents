"""End-to-end tests for built-in bridge actions.

These tests execute real local processes and touch real files inside pytest's
temporary directory.  They never modify system paths: attempts to do so must be
refused by the policy layer before any filesystem/process operation starts.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import app.bridge.audit as audit_mod
from app.bridge.context import ExecutionContext
from app.bridge.executor import bridge_executor
from app.bridge.runner import CancellationToken


@pytest.fixture(autouse=True)
def isolated_audit(tmp_path, monkeypatch):
    """Keep audit/history writes out of the repository's data directory."""
    monkeypatch.setattr(audit_mod, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(audit_mod, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit_mod.audit, "path", tmp_path / "audit.jsonl")
    yield


def run(action: str, params: dict | None = None, *, context: ExecutionContext | None = None) -> dict:
    return bridge_executor.execute(action, params or {}, context=context or ExecutionContext())


def test_executor_envelope_and_unknown_action():
    result = run("echo", {"hello": "world"})
    assert result["ok"] is True
    assert result["action"] == "echo"
    assert result["environment"] == "local"
    assert result["protocol_version"]
    assert result["execution_id"]
    assert result["echo"] == {"hello": "world"}

    previous_legacy = bridge_executor.legacy_handler
    bridge_executor.set_legacy_handler(None)
    try:
        unknown = run("definitely_not_an_action", {})
    finally:
        bridge_executor.set_legacy_handler(previous_legacy)
    assert unknown["ok"] is False
    assert unknown["status"] == "unsupported"


def test_executor_legacy_fallback_is_used_for_unregistered_actions():
    calls: list[tuple[str, dict]] = []

    def legacy(action: str, params: dict) -> dict:
        calls.append((action, params))
        return {"ok": True, "action": action, "detail": "legacy"}

    previous = bridge_executor.legacy_handler
    bridge_executor.set_legacy_handler(legacy)
    try:
        result = run("create_project", {"name": "demo"})
    finally:
        bridge_executor.set_legacy_handler(previous)

    assert calls == [("create_project", {"name": "demo"})]
    assert result["ok"] is True
    assert result["requested_action"] == "create_project"


def test_file_lifecycle_roundtrip(tmp_path):
    target = tmp_path / "notes.txt"
    write = run("write_file", {"path": str(target), "content": "alpha\n"})
    assert write["ok"] is True and write["verified"] is True
    assert target.read_text(encoding="utf-8") == "alpha\n"

    append = run("append_file", {"path": str(target), "content": "beta\n"})
    assert append["ok"] is True and append["verified"] is True

    read = run("read_file", {"path": str(target)})
    assert read["ok"] is True
    assert "alpha" in read["content"] and "beta" in read["content"]
    assert read["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()

    replaced = run("replace_in_file", {"path": str(target), "old": "beta", "new": "gamma", "backup": False})
    assert replaced["ok"] is True and replaced["verified"] is True
    assert "gamma" in target.read_text(encoding="utf-8")

    stat = run("stat_path", {"path": str(target)})
    assert stat["ok"] is True
    assert stat["size_bytes"] == target.stat().st_size
    assert stat["sha256"]


def test_directory_copy_move_delete(tmp_path):
    folder = tmp_path / "work"
    created = run("mkdir", {"folder": str(folder), "parent": ""})
    assert created["ok"] is True and folder.is_dir()

    source = folder / "a.txt"
    source.write_text("payload", encoding="utf-8")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    copied = run("copy_file", {"src": str(source), "dest_folder": str(dest_dir)})
    assert copied["ok"] is True and copied["verified"] is True
    assert (dest_dir / "a.txt").read_text(encoding="utf-8") == "payload"

    moved = run("move_file", {"src": str(dest_dir / "a.txt"), "dest": str(tmp_path / "moved.txt")})
    assert moved["ok"] is True and moved["verified"] is True
    assert (tmp_path / "moved.txt").is_file()
    assert not (dest_dir / "a.txt").exists()

    deleted = run("delete_path", {"path": str(tmp_path / "moved.txt")})
    assert deleted["ok"] is True and deleted["verified"] is True
    assert not (tmp_path / "moved.txt").exists()

    folder_deleted = run("delete_path", {"path": str(folder)})
    assert folder_deleted["ok"] is True and not folder.exists()


def test_glob_search_and_text_search(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "root.txt").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "sub" / "nested.py").write_text("print('needle nested')\n", encoding="utf-8")

    globbed = run("glob_files", {"root": str(tmp_path), "pattern": "**/*"})
    assert globbed["ok"] is True
    names = {row["name"] for row in globbed["files"]}
    assert {"root.txt", "nested.py"} <= names

    searched = run("search_text", {"root": str(tmp_path), "pattern": "needle", "include": "*.txt"})
    assert searched["ok"] is True and searched["count"] == 1
    assert searched["matches"][0]["line"] == 1


def test_write_and_read_protected_paths_are_blocked(tmp_path):
    protected = r"C:\Windows\System32\drivers\etc\bridge-test-should-not-exist.txt"
    write = run("write_file", {"path": protected, "content": "nope"})
    assert write["ok"] is False and write["status"] == "blocked"
    assert write["security"]["code"] == "protected"
    assert not Path(protected).exists()

    check = run("security_check", {"path": r"C:\Windows\System32\config\SAM", "operation": "read"})
    assert check["ok"] is True
    assert check["allowed"] is False
    assert check["decision"]["code"] == "protected"

    check_command = run("security_check", {"command": "format C:"})
    assert check_command["allowed"] is False
    assert check_command["decision"]["risk"] == "critical"


def test_archive_create_and_extract_roundtrip(tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "one.txt").write_text("one", encoding="utf-8")
    (source / "nested" / "two.txt").write_text("two", encoding="utf-8")
    archive = tmp_path / "bundle.zip"

    created = run("archive_create", {"sources": [str(source)], "output": str(archive)})
    assert created["ok"] is True and created["verified"] is True
    assert archive.is_file()

    extracted_to = tmp_path / "extracted"
    extracted = run("archive_extract", {"path": str(archive), "dest": str(extracted_to)})
    assert extracted["ok"] is True and extracted["verified"] is True
    assert (extracted_to / "source" / "one.txt").read_text(encoding="utf-8") == "one"
    assert (extracted_to / "source" / "nested" / "two.txt").read_text(encoding="utf-8") == "two"


def test_archive_traversal_and_protected_sources_are_rejected(tmp_path):
    malicious = tmp_path / "evil.zip"
    with zipfile.ZipFile(malicious, "w") as zf:
        zf.writestr("../escaped.txt", "escaped")
    result = run("archive_extract", {"path": str(malicious), "dest": str(tmp_path / "safe")})
    assert result["ok"] is False
    assert "Unsafe archive member" in result["error"]
    assert not (tmp_path / "escaped.txt").exists()

    protected = run(
        "archive_create",
        {"sources": [r"C:\Windows\System32\config\SAM"], "output": str(tmp_path / "hive.zip")},
    )
    assert protected["ok"] is False and protected["status"] == "blocked"
    assert protected["security"]["code"] == "protected"
    assert not (tmp_path / "hive.zip").exists()


def test_run_command_success_and_policy_block():
    echo = run("run_command", {"cmd": "echo bridge-ok"})
    assert echo["ok"] is True
    assert echo["returncode"] == 0
    assert "bridge-ok" in echo["stdout"]
    assert echo["status"] == "completed"
    assert echo["verified"] is True

    blocked = run("run_command", {"cmd": "format C:"})
    assert blocked["ok"] is False and blocked["status"] == "blocked"
    assert blocked["security"]["risk"] == "critical"


def test_run_command_cancellation_interrupts_process(tmp_path):
    token = CancellationToken()
    timer = threading.Timer(0.6, token.cancel, args=("test cancel",))
    timer.start()
    started = time.time()
    try:
        result = run(
            "run_command",
            {"cmd": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout": 25, "shell": False},
            context=ExecutionContext(cancellation=token),
        )
    finally:
        timer.cancel()
    assert time.time() - started < 15
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["status"] == "cancelled"


def test_run_code_python_and_protected_code(tmp_path):
    result = run("run_code", {"language": "python", "code": "print('bridge-code-ok')", "timeout": 30})
    assert result["ok"] is True, result.get("error")
    assert "bridge-code-ok" in result["stdout"]
    assert result["script"].endswith(".py")
    assert Path(result["script"]).exists() is False  # temp workspace cleaned up

    blocked = run(
        "run_code",
        {
            "language": "python",
            "code": "import shutil; shutil.rmtree(r'C:\\Windows\\System32')",
        },
    )
    assert blocked["ok"] is False and blocked["status"] == "blocked"
    assert blocked["security"]["code"] == "protected"

    unsafe_files = run("run_code", {"language": "python", "code": "print(1)", "files": {"../escape.py": "x"}})
    assert unsafe_files["ok"] is False
    assert "Unsafe relative file path" in unsafe_files["error"]


def test_package_install_rejects_unsafe_specifier():
    result = run("package_install", {"manager": "pip", "packages": ["requests; rm -rf /"]})
    assert result["ok"] is False
    assert "Unsafe package specifier" in result["error"]


def test_managed_process_start_logs_status_stop():
    command = [sys.executable, "-c", "import time; print('proc-alive', flush=True); time.sleep(20)"]
    started = run("process_start", {"cmd": command, "shell": False, "startup_grace": 0.6})
    assert started["ok"] is True, started.get("error")
    process_id = started["process_id"]
    try:
        deadline = time.time() + 10
        logs: dict = {}
        while time.time() < deadline:
            logs = run("process_logs", {"process_id": process_id, "lines": 20})
            if "proc-alive" in str(logs.get("logs") or logs.get("output") or logs):
                break
            time.sleep(0.2)
        assert "proc-alive" in json.dumps(logs)

        status = run("process_status", {"process_id": process_id})
        assert status["ok"] is True and status["running"] is True

        listed = run("process_list", {})
        assert any(row["process_id"] == process_id for row in listed["processes"])
    finally:
        stopped = run("process_stop", {"process_id": process_id})
    assert stopped["ok"] is True and stopped["stopped"] is True
    assert run("process_status", {"process_id": process_id})["running"] is False


class _HTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/json":
            body = json.dumps({"ok": True, "value": 42}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/large":
            body = b"x" * 5000
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/missing":
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(204)
            self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # silence test server
        return


@pytest.fixture
def http_base():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_request_json_and_errors(http_base):
    result = run("http_request", {"url": f"{http_base}/json"})
    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["json"] == {"ok": True, "value": 42}

    missing = run("http_request", {"url": f"{http_base}/missing"})
    assert missing["ok"] is False
    assert missing["status_code"] == 404

    metadata = run("http_request", {"url": "http://169.254.169.254/latest/meta-data"})
    assert metadata["ok"] is False
    assert "metadata" in metadata["error"].lower()

    insecure = run("http_request", {"url": "https://example.com", "verify_tls": False})
    assert insecure["ok"] is False and insecure["status"] == "blocked"


def test_download_file_and_protected_destination(http_base, tmp_path):
    target = tmp_path / "download.bin"
    result = run("download_file", {"url": f"{http_base}/large", "path": str(target)})
    assert result["ok"] is True and result["verified"] is True
    assert target.read_bytes() == b"x" * 5000
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()

    blocked = run(
        "download_file",
        {"url": f"{http_base}/large", "path": r"C:\Windows\System32\bridge-download.bin"},
    )
    assert blocked["ok"] is False and blocked["status"] == "blocked"
    assert not Path(r"C:\Windows\System32\bridge-download.bin").exists()


def test_git_action_runs_in_repository():
    result = run("git", {"cwd": str(Path(__file__).resolve().parents[1]), "subcommand": "status", "args": ["--short"]})
    assert result["ok"] is True, result.get("error")
    assert result["returncode"] == 0
    assert result["verified"] is True


def test_verify_action_checks_evidence(tmp_path):
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("verified payload", encoding="utf-8")
    result = run(
        "verify",
        {
            "result": {"ok": True, "stdout": "hello world", "returncode": 0},
            "checks": [
                {"type": "stdout_contains", "value": "hello"},
                {"type": "exit_code", "equals": 0},
                {"type": "file_exists", "path": str(evidence)},
                {"type": "file_contains", "path": str(evidence), "value": "payload"},
            ],
        },
    )
    assert result["ok"] is True
    assert all(row["ok"] for row in result["verification"]["checks"])

    failed = run(
        "verify",
        {"result": {"ok": True, "stdout": "nope", "returncode": 0}, "checks": [{"type": "stdout_contains", "value": "yes"}]},
    )
    assert failed["ok"] is False and failed["status"] == "failed"


def test_wait_action_honours_cancellation():
    token = CancellationToken()
    token.cancel("stop waiting")
    result = run("wait", {"seconds": 5}, context=ExecutionContext(cancellation=token))
    assert result["ok"] is False
    assert result["status"] == "cancelled"
