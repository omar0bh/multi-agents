import subprocess
import logging
from pathlib import Path

from app.tools.security_guard import is_command_safe

logger = logging.getLogger(__name__)

DEFAULT_CWD = Path(__file__).resolve().parents[2]


def run_safe_command(command: str, cwd: str | Path | None = None, timeout: int = 120) -> dict:
    """Execute developer or installation terminal commands safely under OS protections."""
    is_safe, reason = is_command_safe(command)
    if not is_safe:
        logger.warning("Blocked potentially harmful command: %s (%s)", command, reason)
        return {
            "ok": False,
            "blocked": True,
            "error": reason,
        }

    work_dir = Path(cwd).resolve() if cwd else DEFAULT_CWD
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:].strip(),
            "stderr": proc.stderr[-4000:].strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"Command execution timed out after {timeout} seconds.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }
