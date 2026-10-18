import os
import re
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# Root & critical system directories that CANNOT be deleted, formatted, or overwritten
PROTECTED_SYSTEM_PATHS = frozenset(
    [
        Path("C:/"),
        Path("C:/Windows"),
        Path("C:/Windows/System32"),
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
        Path("C:/ProgramData"),
        Path("C:/Users"),
        Path.home(),  # C:\Users\<Username> itself cannot be deleted
        Path.home() / "AppData",
        Path.home() / "Application Data",
    ]
)

# Dangerous terminal command patterns that could damage the OS
BLOCKED_COMMAND_PATTERNS = [
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
    re.compile(r"\bbcdedit\b", re.IGNORECASE),
    re.compile(r"\brmdir\s+/[sS]\s+/[qQ]\s+[cC]:\\?", re.IGNORECASE),
    re.compile(r"\bdel\s+/[fF]\s+/[sS]\s+/[qQ]\s+[cC]:\\?", re.IGNORECASE),
    re.compile(r"\breg\s+delete\b", re.IGNORECASE),
    re.compile(r"\bshutdown\b", re.IGNORECASE),
    re.compile(r"\bvssadmin\s+delete\s+shadows\b", re.IGNORECASE),
]


def is_path_safe_for_deletion(target_path: Path) -> tuple[bool, str]:
    """Verify that a path is safe to delete and does NOT touch critical OS directories."""
    resolved = target_path.expanduser().resolve()

    # 1. Exact match against protected system roots
    if resolved in PROTECTED_SYSTEM_PATHS:
        return False, f"Protected OS path cannot be modified or deleted: {resolved}"

    # 2. Check if the target is inside Windows, Program Files, or System32
    for protected in (Path("C:/Windows"), Path("C:/Program Files"), Path("C:/Program Files (x86)"), Path.home() / "AppData"):
        try:
            resolved.relative_to(protected)
            return False, f"Cannot touch files inside critical system folder: {protected}"
        except ValueError:
            pass

    # 3. Check if target is a drive root
    if str(resolved).endswith(":\\") or str(resolved).endswith(":/"):
        return False, f"Drive root cannot be deleted: {resolved}"

    return True, "Path is safe."


def is_command_safe(command: str) -> tuple[bool, str]:
    """Inspect shell command to prevent harmful OS operations while allowing dev commands."""
    cmd_lower = command.strip().lower()

    for pattern in BLOCKED_COMMAND_PATTERNS:
        if pattern.search(cmd_lower):
            return False, f"Harmful system command blocked for safety: {pattern.pattern}"

    return True, "Command is safe."
