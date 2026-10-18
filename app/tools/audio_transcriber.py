import logging
from pathlib import Path
import httpx

from app.config import GROQ_API_KEY

logger = logging.getLogger(__name__)

GROQ_AUDIO_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def transcribe_audio_with_groq(
    audio_bytes_or_path: bytes | str | Path,
    filename: str = "voice.ogg",
) -> dict:
    """Transcribe voice note or audio file to text using ultra-fast Groq Whisper (Free Tier)."""
    if not GROQ_API_KEY:
        return {"ok": False, "error": "Missing GROQ_API_KEY in .env."}

    if isinstance(audio_bytes_or_path, (str, Path)):
        p = Path(audio_bytes_or_path).expanduser().resolve()
        if not p.is_file():
            return {"ok": False, "error": f"Audio file not found: {audio_bytes_or_path}"}
        audio_bytes = p.read_bytes()
        filename = p.name
    else:
        audio_bytes = audio_bytes_or_path

    # Use standard multipart form
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }

    files = {
        "file": (filename, audio_bytes, "audio/ogg"),
    }
    data = {
        "model": "whisper-large-v3-turbo",
        "response_format": "json",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(GROQ_AUDIO_URL, headers=headers, files=files, data=data)
            if resp.status_code != 200:
                # Fallback to standard whisper-large-v3
                data["model"] = "whisper-large-v3"
                fallback_resp = client.post(GROQ_AUDIO_URL, headers=headers, files=files, data=data)
                if fallback_resp.status_code == 200:
                    resp = fallback_resp
                else:
                    logger.error("Groq Whisper error: %s", resp.text)
                    return {"ok": False, "error": f"Groq Whisper transcription failed: {resp.text}"}

            result_json = resp.json()
            transcription = result_json.get("text", "").strip()
            return {
                "ok": True,
                "text": transcription,
                "model": data["model"],
            }
    except Exception as exc:
        logger.exception("Failed to transcribe audio with Groq: %s", exc)
        return {"ok": False, "error": str(exc)}
