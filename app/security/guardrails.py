"""Sandbox and guardrail policy for autonomous agent actions.

The guardrails reuse the bridge security policy so browser automation, mail
access, scrapers and outreach all share the same protected-path and command
rules as local command execution.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.bridge.security import (
    APPROVAL_TOKEN_ENV,
    PolicyDecision,
    check_command,
    check_path,
    redact,
    redact_obj,
    sanitized_env,
)


class SandboxPolicy:
    """Fail-closed policy for autonomous agent tool execution."""

    def __init__(self, workspace_roots: list[str | Path] | None = None, approval_token: str | None = None):
        self.workspace_roots = [Path(p).expanduser().resolve() for p in (workspace_roots or [])]
        self.approval_token = approval_token or os.getenv(APPROVAL_TOKEN_ENV)

    def check_path(self, path: str | Path, operation: str = "read") -> PolicyDecision:
        decision = check_path(path, operation, approval_token=self.approval_token)
        if decision.allowed and self.workspace_roots and operation in {"write", "delete", "move", "copy_dest"}:
            target = Path(str(decision.path or path)).expanduser().resolve()
            if not any(target == root or root in target.parents for root in self.workspace_roots):
                return PolicyDecision(
                    False,
                    "high",
                    reason=f"Path is outside the configured agent workspace: {target}",
                    path=str(target),
                    operation=operation,
                    code="workspace_required",
                    rule="agent_workspace",
                )
        return decision

    def check_command(self, command: str | list[str]) -> PolicyDecision:
        return check_command(command, approval_token=self.approval_token, sandbox=True)

    def scrub_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return sanitized_env(extra)

    def redact(self, value: Any) -> Any:
        return redact_obj(value) if isinstance(value, (dict, list, tuple)) else redact(value)


def default_policy() -> SandboxPolicy:
    roots = [Path.cwd(), Path.home() / "agent-workspace"]
    configured = os.getenv("AGENT_WORKSPACE_ROOTS", "")
    for raw in configured.split(os.pathsep):
        raw = raw.strip()
        if raw:
            roots.append(Path(raw).expanduser())
    return SandboxPolicy(workspace_roots=roots)