"""Capability advertisement sent with every bridge heartbeat."""

from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

from app.bridge import cloud as cloud_mod
from app.bridge.audit import history_stats
from app.bridge.runner import resources
from app.bridge.security import security_summary

PROTOCOL_VERSION = "2.0"
BRIDGE_VERSION = "2.0.0"

LEGACY_ACTIONS = [
    "create_project",
    "download_image",
    "screenshot_analyze",
    "desktop_analyze",
    "chof_desktop",
    "get_icons",
    "list_icons",
    "desktop_icons",
    "arrange_desktop",
    "arrange_icons",
    "rattab_desktop",
    "system_cleanup_and_report",
    "pc_performance_cleanup",
    "performance_report",
    "clean_pc",
]


def bridge_capabilities(include_actions: bool = True, include_resources: bool = True) -> dict[str, Any]:
    from app.bridge.actions import actions

    payload: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "bridge_version": BRIDGE_VERSION,
        "hostname": os.environ.get("COMPUTERNAME") or platform.node(),
        "platform": sys.platform,
        "python": platform.python_version(),
        "features": [
            "command_execution",
            "code_execution",
            "file_operations",
            "process_management",
            "http_api_calls",
            "git",
            "workflows",
            "parallel_steps",
            "checkpoints",
            "resume",
            "cancellation",
            "timeouts",
            "retries",
            "result_verification",
            "execution_history",
            "audit_log",
            "secret_redaction",
            "protected_path_enforcement",
            "approval_gating",
            "cloud_delegation",
            "local_cloud_planning",
            "resource_monitoring",
            "background_processes",
        ],
        "security": security_summary(),
        "cloud": cloud_mod.cloud_providers_info(check_availability=False),
        "history": history_stats(),
        "timestamp": time.time(),
        "project_root": str(Path(__file__).resolve().parents[2]),
        "user_home": str(Path.home()),
    }
    if include_actions:
        payload["actions"] = actions.manifest()
        payload["legacy_actions"] = LEGACY_ACTIONS
        payload["action_count"] = len(payload["actions"]) + len(LEGACY_ACTIONS)
    if include_resources:
        payload["capacity"] = resources.capacity()
    return payload
