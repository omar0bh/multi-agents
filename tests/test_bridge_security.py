"""Security policy tests for the local execution bridge.

These tests exercise the policy layer directly (no destructive action is ever
executed) plus a few action-level attempts that must be refused before they
touch the filesystem.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from app.bridge import security
from app.bridge.security import (
    APPROVAL_TOKEN_ENV,
    approval_valid,
    check_command,
    check_path,
    redact,
    redact_obj,
    sanitized_env,
    security_summary,
)


PROTECTED_WRITE_PATHS = [
    r"C:\Windows",
    r"C:\Windows\System32\drivers\etc\hosts",
    r"C:\Windows\System32\config\SAM",
    r"C:\Windows\System32\config\SYSTEM",
    r"C:\Boot\BCD",
    r"C:\Recovery",
    r"C:\pagefile.sys",
    r"C:\\",
]


@pytest.mark.parametrize("path", PROTECTED_WRITE_PATHS)
def test_protected_windows_paths_cannot_be_modified(path):
    decision = check_path(path, "write")
    assert decision.allowed is False
    assert decision.code == "protected"
    assert decision.risk == "critical"


@pytest.mark.parametrize("path", PROTECTED_WRITE_PATHS)
def test_critical_paths_stay_blocked_even_with_approval(path, monkeypatch):
    monkeypatch.setenv(APPROVAL_TOKEN_ENV, "approval-secret")
    decision = check_path(path, "delete", approval_token="approval-secret")
    assert decision.allowed is False
    assert decision.code == "protected"


def test_credential_hives_cannot_be_read():
    for path in (
        r"C:\Windows\System32\config\SAM",
        r"C:\Windows\System32\config\SECURITY",
        r"C:\Windows\System32\config\SYSTEM",
        r"C:\Windows\System32\Microsoft\Protect",
    ):
        decision = check_path(path, "read")
        assert decision.allowed is False
        assert decision.code == "protected"


def test_normal_workspace_and_temp_writes_are_allowed(tmp_path):
    for path in (tmp_path / "a.txt", Path(os.environ.get("TEMP", tmp_path)) / "bridge-test.txt"):
        decision = check_path(path, "write")
        assert decision.allowed is True
        assert decision.requires_approval is False


def test_alternate_data_stream_is_rejected():
    if os.name != "nt":
        pytest.skip("Windows-only path syntax")
    decision = check_path(r"C:\Temp\file.txt:hidden", "write")
    assert decision.allowed is False
    assert decision.code == "protected"


def test_outside_workspace_requires_approval(monkeypatch, tmp_path):
    monkeypatch.delenv(APPROVAL_TOKEN_ENV, raising=False)
    path = r"C:\ProgramData\BridgeUpgradeTest\config.ini"
    decision = check_path(path, "write")
    assert decision.allowed is False
    assert decision.code == "approval_required"
    assert decision.requires_approval is True

    monkeypatch.setenv(APPROVAL_TOKEN_ENV, "approval-secret")
    allowed = check_path(path, "write", approval_token="approval-secret")
    assert allowed.allowed is True
    assert allowed.requires_approval is True

    wrong = check_path(path, "write", approval_token="wrong")
    assert wrong.allowed is False
    assert wrong.code == "approval_required"


def test_sensitive_user_paths_require_approval(monkeypatch):
    monkeypatch.delenv(APPROVAL_TOKEN_ENV, raising=False)
    decision = check_path(Path.home() / ".ssh" / "config", "write")
    assert decision.allowed is False
    assert decision.requires_approval is True
    assert decision.rule == "sensitive_path"


@pytest.mark.parametrize(
    "command",
    [
        "format C:",
        "diskpart",
        "bcdedit /set {default} safeboot minimal",
        "vssadmin delete shadows /all",
        "cipher /w:C:",
        r"reg delete HKLM\SAM /f",
        "netsh advfirewall set allprofiles state off",
        "sc stop windefend",
        "taskkill /im lsass.exe /f",
        "shutdown /s /t 0",
        "takeown /f C:\\Windows /r",
        "rd /s /q C:\\Windows",
        "Set-MpPreference -DisableRealtimeMonitoring $true",
        "dd if=/dev/zero of=/dev/sda",
    ],
)
def test_critical_shell_commands_are_blocked(command):
    decision = check_command(command)
    assert decision.allowed is False
    assert decision.risk == "critical"
    assert decision.code in ("protected", "blocked")


def test_encoded_powershell_is_decoded_or_rejected():
    payload = base64.b64encode("Remove-Item C:\\Windows -Recurse".encode("utf-16-le")).decode("ascii")
    decision = check_command(f"powershell -EncodedCommand {payload}")
    assert decision.allowed is False
    assert decision.code in ("protected", "blocked")


def test_high_risk_command_requires_valid_approval(monkeypatch):
    monkeypatch.delenv(APPROVAL_TOKEN_ENV, raising=False)
    command = r"reg add HKCU\Software\BridgeUpgradeTest /v x /d 1 /f"
    denied = check_command(command)
    assert denied.allowed is False
    assert denied.code == "approval_required"

    monkeypatch.setenv(APPROVAL_TOKEN_ENV, "approval-secret")
    allowed = check_command(command, approval_token="approval-secret")
    assert allowed.allowed is True
    assert allowed.requires_approval is True


def test_destructive_outside_path_requires_approval(monkeypatch):
    monkeypatch.delenv(APPROVAL_TOKEN_ENV, raising=False)
    for command in (
        r"del C:\ProgramData\BridgeUpgradeTest\x.txt",
        r"copy C:\ProgramData\BridgeUpgradeTest\x.txt C:\ProgramData\BridgeUpgradeTest\y.txt",
        r"echo hi > C:\ProgramData\BridgeUpgradeTest\x.txt",
    ):
        decision = check_command(command)
        assert decision.allowed is False
        assert decision.code == "approval_required"


def test_interpreter_code_cannot_bypass_protected_paths(monkeypatch):
    monkeypatch.setenv(APPROVAL_TOKEN_ENV, "approval-secret")
    attempts = [
        r'python -c "import shutil; shutil.rmtree(r\'C:\Windows\System32\')"',
        r'python -c "open(r\'C:\Windows\System32\drivers\etc\hosts\', \'w\').write(\'x\')"',
        r'powershell -Command "Remove-Item C:\Windows\System32 -Recurse"',
        r"dir C:\Windows && del C:\Windows\System32\drivers\etc\hosts",
    ]
    for command in attempts:
        decision = check_command(command, approval_token="approval-secret")
        assert decision.allowed is False, command
        assert decision.code == "protected"


def test_shell_chaining_cannot_hide_outside_write(monkeypatch):
    monkeypatch.delenv(APPROVAL_TOKEN_ENV, raising=False)
    decision = check_command(r"dir C:\Windows && echo x > C:\ProgramData\BridgeUpgradeTest\x")
    assert decision.allowed is False
    assert decision.code in ("protected", "approval_required")


def test_approval_valid_is_constant_time_wrapper(monkeypatch):
    monkeypatch.delenv(APPROVAL_TOKEN_ENV, raising=False)
    assert approval_valid("anything") is False
    monkeypatch.setenv(APPROVAL_TOKEN_ENV, "secret")
    assert approval_valid("secret") is True
    assert approval_valid("Secret") is False
    assert approval_valid("") is False


def test_redaction_removes_env_secrets_and_patterns(monkeypatch):
    monkeypatch.setenv("BRIDGE_TEST_API_KEY", "value-that-must-not-leak")
    text = "token=value-that-must-not-leak key sk-abcdefghijklmnop123456"
    cleaned = redact(text)
    assert "value-that-must-not-leak" not in cleaned
    assert "sk-abcdefghijklmnop123456" not in cleaned
    assert "***REDACTED***" in cleaned

    obj = redact_obj({"password": "hunter2", "nested": {"api_key": "abc"}, "ok": "visible"})
    assert obj["password"] == "***REDACTED***"
    assert obj["nested"]["api_key"] == "***REDACTED***"
    assert obj["ok"] == "visible"


def test_sanitized_env_scrubs_inherited_credentials(monkeypatch):
    monkeypatch.setenv("BRIDGE_TEST_SECRET", "supersecret-value")
    monkeypatch.setenv("ORDINARY_SETTING", "kept")
    env = sanitized_env()
    assert "BRIDGE_TEST_SECRET" not in env
    assert env.get("ORDINARY_SETTING") == "kept"
    assert "PATH" in env or "Path" in env
    explicit = sanitized_env({"MY_API_KEY": "explicit-value"})
    assert explicit["MY_API_KEY"] == "explicit-value"


def test_security_summary_advertises_protections():
    summary = security_summary()
    assert summary["child_env_scrubbed"] is True
    assert summary["critical_command_rules"]
    assert summary["immutable_roots"]
    assert summary["protected_read_paths"]
