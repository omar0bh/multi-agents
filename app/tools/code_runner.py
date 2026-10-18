"""Basic Python runner. This is not a secure sandbox."""
import subprocess
import sys
import tempfile
from pathlib import Path

def run_python(code: str, timeout: int = 5) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / "script.py"
        script.write_text(code, encoding="utf-8")

        try:
            result = subprocess.run(
                [sys.executable, "-I", str(script)],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=folder,
                check=False,
            )

            return {
                "returncode": result.returncode,
                "stdout": result.stdout[-5000:],
                "stderr": result.stderr[-5000:],
                "timed_out": False,
            }

        except subprocess.TimeoutExpired:
            return {
                "returncode": None,
                "stdout": "",
                "stderr": "Execution timed out.",
                "timed_out": True,
            }
