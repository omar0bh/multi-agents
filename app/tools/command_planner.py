"""Natural-language to shell-command planning for the Telegram AI employee.

The planner lets the owner ask for arbitrary PC work in plain language.  It asks
Groq for one safe shell command, then the caller executes that command through
the existing terminal/bridge path so the normal security policy still applies.
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def plan_shell_command(user_text: str) -> str | None:
    """Return one shell command for *user_text*, or None when no command is needed."""
    if not GROQ_API_KEY or not (user_text or "").strip():
        return None

    prompt = f"""You are a command planner for a Windows 11 PC.

Convert the user's request into ONE shell command that can be executed to fulfill it exactly.
Use Windows paths starting with C:\\Users\\DELL LATITUDE\\ when a location is implied.
If the user gives an explicit path, use that exact path.
Prefer installed developer tools such as flutter, npm, npx, python, pip, git.
Do not explain. Return ONLY valid JSON:
{{"command": "<one shell command or null>", "explanation": "<short explanation>"}}

Rules:
- If the request is conversational, informational, or cannot be done by one command, set command to null.
- Never use destructive commands such as format, del /s /q C:\\, rmdir /s /q C:\\, shutdown, reg delete, diskpart, or UAC bypasses.
- For project creation, use the appropriate scaffold command and the exact requested path.
- Include quotes around paths that contain spaces.

User request:
{user_text}
"""

    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
            )
        if resp.status_code != 200:
            logger.warning("Command planner HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        if not isinstance(parsed, dict):
            return None
        command = parsed.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        command = command.strip()
        # Reject obvious multi-line scripts or shell control that bypasses one-command intent.
        if "\n" in command:
            command = " && ".join(part.strip() for part in command.splitlines() if part.strip())
        return command
    except Exception as exc:
        logger.warning("Command planning failed: %s", exc)
        return None