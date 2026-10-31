"""Central security policy for the local execution bridge.

This module is deliberately independent from the LLM.  Every action that can
read, write, delete, or execute something goes through these checks.  The policy
is fail-closed for high-impact operations and *never* allows modifications to
critical Windows infrastructure, even when an approval token is supplied.

Design goals
------------
* Normal user-level development work (home directory, project, temp) is allowed.
* Destructive operations outside those roots require an explicit approval token.
* Critical OS paths and commands are denied unconditionally.
* Secrets are scrubbed from results, logs and child-process environments.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import ntpath
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]

APPROVAL_TOKEN_ENV = "BRIDGE_APPROVAL_TOKEN"
ALLOWED_ROOTS_ENV = "BRIDGE_ALLOWED_ROOTS"
MAX_COMMAND_CHARS = int(os.getenv("BRIDGE_MAX_COMMAND_CHARS", "32768"))

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SYSTEM_ROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
PROGRAM_FILES = [
    Path(p)
    for p in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramW6432", r"C:\Program Files"),
    )
    if p
]
PROGRAM_DATA = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
USER_HOME = Path.home()
TEMP_ROOT = Path(tempfile.gettempdir())


def _system_drive() -> Path:
    """Return the system drive as an absolute root (``C:\\``, not ``C:``).

    ``Path("C:") / "Boot"`` is a *drive-relative* path (``C:Boot``) and would
    resolve against the process working directory.  Always anchor with a
    separator so protected paths can never be shadowed by the cwd.
    """
    raw = os.environ.get("SystemDrive", "C:").strip()
    if not raw:
        raw = "C:"
    if not raw.endswith(("\\", "/")):
        raw += "\\"
    return Path(raw)


SYSTEM_DRIVE = _system_drive()
USERS_ROOT = SYSTEM_DRIVE / "Users"

# Written/deleted paths under these roots are always refused.  These are OS
# components: protected binaries, boot/recovery state, registry hives, etc.
IMMUTABLE_WRITE_ROOTS: tuple[Path, ...] = (
    SYSTEM_ROOT,
    SYSTEM_DRIVE / "Boot",
    SYSTEM_DRIVE / "EFI",
    SYSTEM_DRIVE / "Recovery",
    SYSTEM_DRIVE / "System Volume Information",
    SYSTEM_DRIVE / "$Recycle.Bin",
    SYSTEM_DRIVE / "PerfLogs",
)

# Important OS files outside the Windows directory.
IMMUTABLE_WRITE_FILES: tuple[Path, ...] = tuple(
    p
    for p in (
        SYSTEM_DRIVE / "pagefile.sys",
        SYSTEM_DRIVE / "hiberfil.sys",
        SYSTEM_DRIVE / "swapfile.sys",
        SYSTEM_DRIVE / "bootmgr",
        SYSTEM_DRIVE / "BOOTNXT",
        SYSTEM_ROOT / "System32" / "config" / "SAM",
        SYSTEM_ROOT / "System32" / "config" / "SECURITY",
        SYSTEM_ROOT / "System32" / "config" / "SYSTEM",
        SYSTEM_ROOT / "System32" / "config" / "SOFTWARE",
        SYSTEM_ROOT / "System32" / "config" / "BCD-Template",
        SYSTEM_ROOT / "System32" / "drivers" / "etc" / "hosts",
        SYSTEM_ROOT / "System32" / "drivers" / "etc" / "networks",
        SYSTEM_ROOT / "win.ini",
        SYSTEM_ROOT / "system.ini",
        SYSTEM_ROOT / "Boot" / "BCD",
    )
)

# Access to these paths is blocked even for reads (credential/session material).
PROTECTED_READ_PATHS: tuple[Path, ...] = tuple(
    p
    for p in (
        SYSTEM_ROOT / "System32" / "config" / "SAM",
        SYSTEM_ROOT / "System32" / "config" / "SECURITY",
        SYSTEM_ROOT / "System32" / "config" / "SYSTEM",
        SYSTEM_ROOT / "System32" / "config" / "SOFTWARE",
        SYSTEM_ROOT / "System32" / "config" / "RegBack",
        SYSTEM_ROOT / "System32" / "Microsoft" / "Protect",
        USER_HOME / "AppData" / "Roaming" / "Microsoft" / "Protect",
    )
)

# Writes here are allowed only with an approval token (admin/system scope).
APPROVAL_WRITE_ROOTS: tuple[Path, ...] = tuple(PROGRAM_FILES) + (
    PROGRAM_DATA,
    USERS_ROOT,
    USER_HOME,
    USER_HOME / ".ssh",
    USER_HOME / ".aws",
    USER_HOME / ".azure",
    USER_HOME / ".config",
    USER_HOME / "AppData" / "Roaming" / "Microsoft" / "Crypto",
)

# Sensitive user files where writes should be confirmed first.
SENSITIVE_USER_PATHS: tuple[Path, ...] = (
    USER_HOME / ".ssh",
    USER_HOME / ".aws",
    USER_HOME / ".azure",
    USER_HOME / ".gnupg",
    USER_HOME / "AppData" / "Roaming" / "Microsoft" / "Crypto",
    USER_HOME / "AppData" / "Roaming" / "Microsoft" / "Protect",
)


def _configured_roots() -> tuple[Path, ...]:
    roots = [PROJECT_ROOT, USER_HOME, TEMP_ROOT]
    for raw in os.getenv(ALLOWED_ROOTS_ENV, "").split(os.pathsep):
        raw = raw.strip()
        if raw:
            try:
                roots.append(Path(os.path.expandvars(raw)).expanduser())
            except Exception:
                continue
    return tuple(roots)


ALLOWED_WRITE_ROOTS = _configured_roots()


def _norm(path: Path | str) -> Path:
    p = Path(os.path.expandvars(str(path))).expanduser()
    try:
        p = p.resolve(strict=False)
    except (OSError, RuntimeError):
        # Broken junctions / inaccessible paths: use the lexical absolute path.
        try:
            p = Path(os.path.abspath(str(p)))
        except Exception:
            pass
    return p


def _contains(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def is_under(path: Path, roots: Iterable[Path]) -> bool:
    return any(_same(path, root) or _contains(root, path) for root in roots)


def is_drive_root(path: Path) -> bool:
    text = os.path.normpath(str(path))
    return len(text) <= 3 and re.match(r"^[A-Za-z]:\\?$", text) is not None


def looks_like_windows_path(token: str) -> bool:
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", token)
        or token.startswith("\\\\")
        or token.lower().startswith(("\\windows", "%systemroot%", "$env:systemroot"))
    )


@dataclass
class PolicyDecision:
    allowed: bool
    risk: str = "low"
    requires_approval: bool = False
    reason: str = ""
    path: str = ""
    operation: str = "read"
    code: str = "ok"
    rule: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "risk": self.risk,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
            "path": self.path,
            "operation": self.operation,
            "code": self.code,
            "rule": self.rule,
            "metadata": self.metadata,
        }


def _approval_valid(token: str | None) -> bool:
    expected = os.getenv(APPROVAL_TOKEN_ENV, "")
    if not expected or not token:
        return False
    return hmac.compare_digest(str(token), expected)


def approval_valid(token: str | None) -> bool:
    """Public wrapper used by actions that need an explicit admin approval."""
    return _approval_valid(token)


def resolve_path(path: str | Path, base: str | Path | None = None) -> Path:
    raw = str(path).strip().strip("'\"`")
    if not raw:
        return _norm(base or ".")
    p = Path(os.path.expandvars(raw)).expanduser()
    if not p.is_absolute():
        p = Path(base or os.getcwd()) / p
    return _norm(p)


def check_path(
    path: str | Path,
    operation: str = "read",
    *,
    base: str | Path | None = None,
    approval_token: str | None = None,
    allow_missing: bool = True,
) -> PolicyDecision:
    """Validate a filesystem operation.

    operations: read, write, delete, move, copy_source, copy_dest, execute
    """
    operation = (operation or "read").lower().strip()
    if operation in ("create", "append", "mkdir", "copy_dest", "move_dest"):
        operation = "write"
    if operation in ("remove", "rm", "rmdir", "unlink", "move", "move_source"):
        operation = "delete"
    if operation == "copy_source":
        # Copying reads the source; the destination is checked separately.
        operation = "read"

    try:
        target = resolve_path(path, base)
    except Exception as exc:
        return PolicyDecision(False, "high", reason=f"Invalid path: {exc}", path=str(path), operation=operation, code="invalid_path")

    decision = PolicyDecision(True, "low", path=str(target), operation=operation)

    if "\x00" in str(path):
        return PolicyDecision(False, "critical", reason="NUL byte in path", path=str(target), operation=operation, code="invalid_path")
    # Windows alternate data streams are not needed by the bridge and are a
    # common protection-bypass trick.
    if os.name == "nt":
        drive, tail = ntpath.splitdrive(str(target))
        if ":" in tail:
            return PolicyDecision(False, "critical", reason="Alternate data streams are not permitted", path=str(target), operation=operation, code="protected")

    protected_read = is_under(target, PROTECTED_READ_PATHS)
    immutable = is_under(target, IMMUTABLE_WRITE_ROOTS) or any(_same(target, p) for p in IMMUTABLE_WRITE_FILES)

    if operation == "read":
        if protected_read:
            return PolicyDecision(False, "critical", reason="Reading protected credential/session stores is not permitted", path=str(target), operation=operation, code="protected")
        return decision

    if operation == "execute":
        if is_under(target, [SYSTEM_ROOT] + list(PROGRAM_FILES) + USER_HOME / "AppData" / "Roaming" / "Microsoft"):
            decision.risk = "medium"
        return decision

    if immutable or is_drive_root(target):
        return PolicyDecision(
            False,
            "critical",
            reason=f"Protected Windows/OS path cannot be modified: {target}",
            path=str(target),
            operation=operation,
            code="protected",
            rule="immutable_path",
        )

    if operation == "delete":
        protected_delete_roots = (
            USERS_ROOT,
            USER_HOME,
            PROGRAM_DATA,
            *PROGRAM_FILES,
            USER_HOME / "AppData",
        )
        if any(_same(target, root) for root in protected_delete_roots):
            return PolicyDecision(False, "critical", reason=f"Refusing to delete a root directory: {target}", path=str(target), operation=operation, code="protected", rule="root_delete")
        decision.risk = "medium" if is_under(target, ALLOWED_WRITE_ROOTS) else "high"

    if any(_same(target, p) or _contains(p, target) for p in SENSITIVE_USER_PATHS):
        decision.requires_approval = True
        decision.risk = "high"
        decision.reason = "Sensitive credential/configuration path requires approval"
        decision.rule = "sensitive_path"

    if is_under(target, PROGRAM_FILES + [PROGRAM_DATA]):
        decision.requires_approval = True
        decision.risk = "high"
        decision.reason = "System-wide installation/configuration path requires approval"
        decision.rule = "system_install_path"

    if not is_under(target, ALLOWED_WRITE_ROOTS):
        decision.requires_approval = True
        decision.risk = "high" if decision.risk == "low" else decision.risk
        decision.reason = decision.reason or "Path is outside the approved workspace roots"
        decision.rule = decision.rule or "outside_allowed_roots"

    if decision.requires_approval and not _approval_valid(approval_token):
        return PolicyDecision(
            False,
            decision.risk,
            True,
            reason=(decision.reason or "Approval required") + " (set BRIDGE_APPROVAL_TOKEN and pass approval_token)",
            path=str(target),
            operation=operation,
            code="approval_required",
            rule=decision.rule,
        )

    return decision


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

CRITICAL_COMMAND_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("disk_format", re.compile(r"(?:^|[;&|]\s*)(?:format(?:\.com)?\s+[a-z]:|diskpart\b|clean\s+all\b)", re.I), "Disk formatting/partitioning is blocked"),
    ("boot_config", re.compile(r"\b(?:bcdedit|bootrec|bootsect|reagentc|mountvol|dism\s+/image|wmic\s+bootconfig)\b", re.I), "Boot/recovery configuration is blocked"),
    ("shadow_copy", re.compile(r"\bvssadmin\s+(?:delete|resize)\s+shadows?\b|\bwbadmin\s+delete\b", re.I), "Deleting backup/shadow copies is blocked"),
    ("secure_erase", re.compile(r"\bcipher(?:\.exe)?\s+/w\b", re.I), "Secure-erase operations are blocked"),
    ("defender_tamper", re.compile(r"(?:set|add)-mppreference\b.*(?:disablerealtimemonitoring|disableioavprotection|exclusionpath|exclusionextension|enablecontrolledfolderaccess\s+disabled)", re.I), "Windows Defender tampering is blocked"),
    ("firewall_disable", re.compile(r"\bnetsh\s+(?:advfirewall|firewall)\b.*\b(?:off|disable)\b", re.I), "Disabling the Windows firewall is blocked"),
    ("critical_service", re.compile(r"\b(?:sc(?:\.exe)?|net)\s+(?:stop|delete|config)\s+(?:windefend|wuauserv|eventlog|lsm|samss|rpcss|dcomlaunch|plugplay|power|themes|winmgmt)\b", re.I), "Stopping/deleting critical Windows services is blocked"),
    ("critical_process", re.compile(r"\btaskkill\b.*\b(?:/im\s+)?(?:svchost|wininit|winlogon|csrss|smss|services|lsass|explorer)\.exe\b", re.I), "Killing critical Windows processes is blocked"),
    ("power_state", re.compile(r"\b(?:shutdown|restart-computer|stop-computer)\b", re.I), "Power-state changes are blocked by the bridge"),
    ("registry_hive", re.compile(r"\breg(?:\.exe)?\s+(?:add|delete|import|restore|load|unload)\b.*\b(?:hklm\\(?:sam|security|system|bcd00000000)|hkey_local_machine\\(?:sam|security|system|bcd00000000))\b", re.I), "Protected registry hives are blocked"),
    ("uac_bypass", re.compile(r"(?:-verb\s+runas|fodhelper|eventvwr(?:\.exe)?\s+/s|computerdefaults|sdclt|cmstp(?:\.exe)?|mshta\s+https?://)", re.I), "UAC bypass patterns are blocked"),
    ("encoded_powershell", re.compile(r"\b(?:powershell|pwsh)(?:\.exe)?\b.*\s-(?:e|ec|en|enc|encoded|encodedcommand)\s+[A-Za-z0-9+/=]{16,}", re.I), "Encoded PowerShell commands are blocked"),
    ("raw_disk", re.compile(r"\b(?:dd\s+if=|wmic\s+diskdrive\b.*\bdelete\b|fsutil\s+file\s+setzerodata\b)", re.I), "Raw disk writes are blocked"),
    ("takeown_system", re.compile(r"\b(?:takeown|icacls)\b.*(?:%systemroot%|[a-z]:\\windows)\b", re.I), "Changing ownership/ACLs of Windows is blocked"),
    ("delete_windows", re.compile(r"\b(?:rd|rmdir|del|erase|remove-item)\b.*(?:%systemroot%|[a-z]:\\windows(?:\s|\\|\"|$))", re.I), "Deleting the Windows directory is blocked"),
]

HIGH_RISK_COMMAND_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("registry_write", re.compile(r"\breg(?:\.exe)?\s+(?:add|delete|import|restore|load|unload)\b", re.I), "Registry modification"),
    ("service_change", re.compile(r"\b(?:sc(?:\.exe)?|net)\s+(?:create|start|stop|delete|config)\b", re.I), "Windows service modification"),
    ("scheduled_task", re.compile(r"\bschtasks(?:\.exe)?\s+/(?:create|delete|change|run)\b", re.I), "Scheduled task modification"),
    ("firewall_change", re.compile(r"\bnetsh\s+(?:advfirewall|firewall|interface)\b.*\b(?:add|delete|set|reset)\b", re.I), "Firewall/network configuration change"),
    ("user_account", re.compile(r"\bnet\s+user\b|\bnet\s+localgroup\b|\bwhoami\s+/", re.I), "User account/group modification"),
    ("system_integrity", re.compile(r"\b(?:sfc|dism)\b|\bchkdsk\b|\bdefrag\b|\bmanage-bde\b|\bpowercfg\b", re.I), "System integrity/power configuration change"),
    ("destructive_delete", re.compile(r"\b(?:rm|rmdir|rd|del|erase|remove-item)\b.*(?:-r(?:ecurse)?\b|/s\b)", re.I), "Recursive deletion"),
    ("git_destructive", re.compile(r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s+\.|branch\s+-D)\b", re.I), "Destructive git operation"),
    ("package_system", re.compile(r"\b(?:winget|choco|scoop)\s+(?:install|uninstall|upgrade)\b|\bmsiexec(?:\.exe)?\s+/i\b", re.I), "System package installation/removal"),
    ("elevation", re.compile(r"\b(?:runas|start-process)\b.*\b(?:-verb\s+runas|/user:)\b", re.I), "Privilege elevation"),
    ("mass_move", re.compile(r"\b(?:move|robocopy|xcopy)\b", re.I), "Bulk file move/copy"),
    ("network_change", re.compile(r"\b(?:ipconfig\s+/(?:release|renew|flushdns)|netsh\s+interface|route\s+(?:add|delete)|arp\s+-d)\b", re.I), "Network configuration change"),
]

DESTRUCTIVE_VERBS = re.compile(
    r"\b(?:del|erase|rmdir|rd|rm|remove-item|move|mv|copy|cp|robocopy|xcopy|ren|rename|"
    r"set-content|add-content|out-file|new-item|tee|attrib|icacls|takeown|reg\s+(?:add|delete)|"
    r"sc(?:\.exe)?\s+(?:create|delete|config)|schtasks(?:\.exe)?\s+/create|chmod|chown)\b",
    re.I,
)

# Commands that are read-only and should not be treated as touching paths.
READ_ONLY_COMMANDS = re.compile(
    r"^\s*(?:dir|ls|type|cat|get-content|get-childitem|gci|where|which|findstr|grep|"
    r"git\s+(?:status|diff|log|show|branch|remote|rev-parse)|python(?:\.exe)?\s+-c\s+['\"]print|"
    r"echo|hostname|whoami|ipconfig|systeminfo|ver|pwd|cd)\b",
    re.I,
)

# Patterns that indicate *code* may mutate the filesystem.  They are used by
# ``check_command(..., sandbox=True)`` together with the path policy so a
# ``run_code`` script cannot bypass protected Windows paths with
# ``shutil.rmtree`` / ``Path.unlink`` / ``write_text`` / ``open(..., 'w')``.
CODE_MUTATION_RE = re.compile(
    r"(?:"
    r"shutil\.(?:rmtree|move|copy|copy2|copytree)|"
    r"os\.(?:remove|unlink|rmdir|removedirs|rename|replace|makedirs|mkdir|chmod|chown)|"
    r"\.unlink\s*\(|\.rmdir\s*\(|\.write_text\s*\(|\.write_bytes\s*\(|"
    r"open\s*\([^)]*?,\s*\\*['\"][wax+][bft]?\\*['\"]|"
    r"winreg\.(?:SetValue|SetValueEx|CreateKey|CreateKeyEx|DeleteKey|DeleteValue)|"
    r"subprocess\.(?:run|call|Popen|check_call|check_output)|os\.(?:system|popen)|"
    r"ctypes\.(?:windll|WinDLL|CDLL)|"
    r"set-content|add-content|out-file|remove-item|new-item|"
    r"rm\s+-|del\s+|rmdir\s+|rd\s+|erase\s+|move\s+-|copy\s+"
    r")",
    re.I,
)


def _code_path_rule(code: str, approval_token: str | None) -> tuple[PolicyDecision | None, str, bool]:
    """Inspect a script before execution.

    Returns ``(blocking_decision, highest_risk, requires_approval)``.  Critical
    OS paths are blocked outright; writing to system-wide/out-of-workspace
    paths still needs an approval token.
    """
    tokens: list[str] = list(_extract_path_tokens(code))
    # Python/C string literals often contain doubled backslashes.
    for match in re.finditer(r"[A-Za-z]:[\\/][^\"'\s;)]*", code):
        tokens.append(_clean_token(match.group(0).replace("\\\\", "\\")))
    for match in re.finditer(r"%[A-Za-z_]+%[\\/][^\"'\s;)]*", code):
        tokens.append(_clean_token(match.group(0)))

    mutates = bool(CODE_MUTATION_RE.search(code))
    highest = "low"
    requires_approval = False
    for token in dict.fromkeys(tokens):
        if not looks_like_windows_path(token):
            continue
        decision = check_path(token, "write" if mutates else "read", approval_token=approval_token)
        if not decision.allowed and decision.code != "approval_required":
            return decision, decision.risk, decision.requires_approval
        if not decision.allowed or decision.requires_approval:
            # Missing approval is merged by the caller; only protected/immutable
            # paths are reported as unconditionally blocking decisions.
            requires_approval = True
            highest = "high"
    return None, highest, requires_approval


@dataclass
class CommandDecision:
    allowed: bool
    risk: str = "low"
    requires_approval: bool = False
    reason: str = "Command is allowed"
    code: str = "ok"
    rule: str = ""
    command: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "risk": self.risk,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
            "code": self.code,
            "rule": self.rule,
            "command": self.command,
            "metadata": self.metadata,
        }


def _decode_powershell(command: str) -> str | None:
    """Best-effort decode of -EncodedCommand for inspection (never executed)."""
    m = re.search(r"\s-(?:e|ec|en|enc|encoded|encodedcommand)\s+([A-Za-z0-9+/=]{16,})", command, re.I)
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4))
        return raw.decode("utf-16-le", errors="replace")
    except (binascii.Error, ValueError, UnicodeError):
        return None


def _extract_path_tokens(command: str) -> list[str]:
    tokens = re.findall(r'(?:"([^"]+)"|\'([^\']+)\'|((?:[A-Za-z]:[\\/]|\\\\)[^\s;&|]+|%[A-Za-z_]+%[\\/][^\s;&|]*))', command)
    out: list[str] = []
    for groups in tokens:
        token = next((g for g in groups if g), "")
        if token:
            out.append(_clean_token(token))
    return out


def _clean_token(token: str) -> str:
    """Trim punctuation that commonly follows a path inside code/commands."""
    cleaned = str(token).strip()
    while cleaned and cleaned[-1] in "\"',;":
        cleaned = cleaned[:-1]
    return cleaned


def _path_rule_for_command(command: str, approval_token: str | None) -> tuple[PolicyDecision | None, str, bool]:
    """Check paths referenced by destructive commands.

    Returns (blocking_decision, highest_risk, requires_approval).  A blocking
    decision is only returned when the path policy outright refuses access
    (e.g. C:\\Windows).  Approval requirements are merged by the caller.
    """
    if not re.search(r"[;&|<>]", command) and READ_ONLY_COMMANDS.search(command):
        return None, "low", False
    if not DESTRUCTIVE_VERBS.search(command) and ">" not in command:
        return None, "low", False

    highest = "low"
    requires_approval = False
    for token in _extract_path_tokens(command):
        if not looks_like_windows_path(token):
            continue
        decision = check_path(token, "write", approval_token=approval_token)
        if not decision.allowed and decision.code == "protected":
            return decision, decision.risk, decision.requires_approval
        if decision.requires_approval:
            requires_approval = True
        if decision.risk in ("high", "critical"):
            highest = "high"
    return None, highest, requires_approval


def check_command(
    command: str | list[str],
    *,
    cwd: str | Path | None = None,
    approval_token: str | None = None,
    sandbox: bool = False,
) -> CommandDecision:
    """Validate a command before execution.

    Critical OS operations are always refused.  High-impact but recoverable
    operations require ``BRIDGE_APPROVAL_TOKEN`` to be configured and supplied.
    """
    text = command if isinstance(command, str) else " ".join(str(x) for x in command)
    text = text.strip()
    if not text:
        return CommandDecision(False, "low", reason="Empty command", code="empty")
    if len(text) > MAX_COMMAND_CHARS:
        return CommandDecision(False, "high", reason=f"Command exceeds {MAX_COMMAND_CHARS} characters", code="too_long")

    decoded = _decode_powershell(text)
    if decoded:
        nested = check_command(decoded, cwd=cwd, approval_token=approval_token, sandbox=sandbox)
        if not nested.allowed:
            return CommandDecision(False, "critical", reason="Encoded command decodes to a blocked command", code="blocked", rule="encoded_nested", command=text)
        return CommandDecision(False, "critical", reason="Encoded PowerShell commands are not accepted", code="blocked", rule="encoded_powershell", command=text)

    for rule, pattern, reason in CRITICAL_COMMAND_RULES:
        if pattern.search(text):
            return CommandDecision(False, "critical", reason=reason, code="protected", rule=rule, command=text)

    # Destructive commands that touch an immutable OS path are blocked even
    # with a valid approval token.
    path_decision, path_risk, path_requires_approval = _path_rule_for_command(text, approval_token)
    if path_decision:
        return CommandDecision(False, path_decision.risk, reason=path_decision.reason, code="protected", rule=path_decision.rule or "protected_path", command=text)

    risk = path_risk
    rule = ""
    requires_approval = False
    reason_text = "Command is allowed"
    code_requires = False
    if path_requires_approval:
        requires_approval = True
        risk = "high"
        reason_text = "Command modifies a path outside the approved workspace roots"
        rule = "outside_allowed_roots"

    # Inspect interpreter/code-like commands even when the caller did not ask
    # for explicit sandboxing: ``python -c``/encoded scripts must not be able
    # to bypass the path policy.  The scan only blocks protected paths and
    # escalates writes outside the approved roots to approval.
    code_decision, code_risk, code_requires = _code_path_rule(text, approval_token)
    if code_decision:
        return CommandDecision(
            False,
            code_decision.risk,
            reason=code_decision.reason,
            code="protected",
            rule=code_decision.rule or "protected_path",
            command=text,
        )
    if code_risk in ("high", "critical") and risk == "low":
        risk = code_risk
    requires_approval = requires_approval or code_requires

    for name, pattern, reason in HIGH_RISK_COMMAND_RULES:
        if pattern.search(text):
            # Destructive shell words are common in ordinary source code.  In
            # sandbox mode they only matter if a protected/system path is
            # involved (handled above by _code_path_rule).
            if sandbox and name in ("mass_move", "destructive_delete", "git_destructive"):
                continue
            requires_approval = True
            if risk == "low":
                risk = "high"
            rule = name
            reason_text = reason
            break

    if sandbox:
        # Critical and immutable-path checks above remain unconditional.
        requires_approval = requires_approval or code_requires

    if requires_approval and not _approval_valid(approval_token):
        return CommandDecision(
            False,
            risk,
            True,
            reason=f"{reason_text} requires approval (set {APPROVAL_TOKEN_ENV} and pass approval_token)",
            code="approval_required",
            rule=rule or "high_risk",
            command=text,
        )

    return CommandDecision(True, risk, requires_approval, reason_text, "ok", rule, text)


def is_command_safe(command: str) -> tuple[bool, str]:
    """Backward-compatible wrapper used by older tools."""
    decision = check_command(command)
    return decision.allowed, decision.reason


def is_path_safe_for_deletion(target_path: Path | str) -> tuple[bool, str]:
    """Backward-compatible wrapper used by older tools."""
    decision = check_path(target_path, "delete", allow_missing=False)
    return decision.allowed, decision.reason


# ---------------------------------------------------------------------------
# Secret redaction / child environment
# ---------------------------------------------------------------------------

_SENSITIVE_KEY_RE = re.compile(r"(?:secret|token|passw|pwd|api[_-]?key|private[_-]?key|credential|auth|session|cookie|dsn|connection[_-]?string)", re.I)
_SECRET_VALUE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{6,}"),
]


def sensitive_env_values() -> list[str]:
    values: list[str] = []
    for key, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if _SENSITIVE_KEY_RE.search(key) and value.lower() not in {"true", "false", "none", "null"}:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)[:64]


def redact(text: Any, max_chars: int | None = None) -> str:
    """Remove known secrets and common credential patterns from text."""
    if text is None:
        return ""
    result = str(text)
    for value in sensitive_env_values():
        if value and value in result:
            result = result.replace(value, "***REDACTED***")
    for pattern in _SECRET_VALUE_PATTERNS:
        result = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "***REDACTED***", result)
    if max_chars is not None and len(result) > max_chars:
        head = max_chars * 3 // 4
        tail = max_chars - head
        result = result[:head] + "\n... [truncated] ...\n" + result[-tail:]
    return result


def redact_obj(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-serialisable structure."""
    if _depth > 8:
        return "***MAX_DEPTH***"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_KEY_RE.search(str(key)) and not isinstance(item, (dict, list)):
                out[key] = "***REDACTED***" if item not in (None, "") else item
            else:
                out[key] = redact_obj(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [redact_obj(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def sanitized_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return an inherited environment with credentials removed.

    Developer tooling still gets PATH, SystemRoot, TEMP, HOME, etc., but API
    keys and bridge secrets are not exposed to arbitrary child processes.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if _SENSITIVE_KEY_RE.search(key):
            continue
        if key in {"BRIDGE_SECRET", "BRIDGE_APPROVAL_TOKEN", "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN"}:
            continue
        env[key] = value
    if extra:
        for key, value in extra.items():
            if value is None:
                continue
            if _SENSITIVE_KEY_RE.search(str(key)):
                # Explicit per-command secrets are permitted (e.g. an API call
                # needs its own token), but never inherited by accident.
                env[str(key)] = str(value)
            else:
                env[str(key)] = str(value)
    return env


def security_summary() -> dict[str, Any]:
    return {
        "approval_configured": bool(os.getenv(APPROVAL_TOKEN_ENV)),
        "allowed_write_roots": [str(p) for p in ALLOWED_WRITE_ROOTS],
        "immutable_roots": [str(p) for p in IMMUTABLE_WRITE_ROOTS],
        "protected_read_paths": [str(p) for p in PROTECTED_READ_PATHS],
        "critical_command_rules": [name for name, _, _ in CRITICAL_COMMAND_RULES],
        "high_risk_command_rules": [name for name, _, _ in HIGH_RISK_COMMAND_RULES],
        "child_env_scrubbed": True,
        "approval_env": APPROVAL_TOKEN_ENV,
        "platform": os.name,
    }
