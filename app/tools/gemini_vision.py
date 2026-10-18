import base64
import logging
from pathlib import Path

import httpx

from app.config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

GEMINI_MODEL = "models/gemini-3.8-flash"


def analyze_image_with_gemini(
    image_bytes_or_path: bytes | str | Path,
    prompt: str = "Analyze this image in detail and extract all key information.",
    mime_type: str = "image/jpeg",
) -> dict:
    """Send an image to Gemini Vision API and return the extracted analysis."""
    if not GEMINI_API_KEY:
        return {
            "ok": False,
            "error": "Missing GEMINI_API_KEY in .env.",
        }

    # Prepare raw bytes
    if isinstance(image_bytes_or_path, (str, Path)):
        p = Path(image_bytes_or_path).expanduser().resolve()
        if not p.is_file():
            return {"ok": False, "error": f"Image file not found: {image_bytes_or_path}"}
        image_bytes = p.read_bytes()
        suffix = p.suffix.lower()
        if suffix == ".png":
            mime_type = "image/png"
        elif suffix in (".jpg", ".jpeg"):
            mime_type = "image/jpeg"
        elif suffix == ".webp":
            mime_type = "image/webp"
    else:
        image_bytes = image_bytes_or_path

    b64_data = base64.b64encode(image_bytes).decode("utf-8")

    url = f"https://generativelanguage.googleapis.com/v1beta/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": b64_data,
                        }
                    },
                ]
            }
        ]
    }

    try:
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error("Gemini Vision HTTP %s: %s", resp.status_code, resp.text)
                return {
                    "ok": False,
                    "error": f"Gemini API returned status {resp.status_code}: {resp.text[:300]}",
                }

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return {"ok": False, "error": "No candidates returned by Gemini."}

            parts = candidates[0].get("content", {}).get("parts", [])
            text_result = "\n".join(part.get("text", "") for part in parts if "text" in part).strip()

            return {
                "ok": True,
                "model": GEMINI_MODEL,
                "analysis": text_result,
            }
    except Exception as exc:
        logger.exception("Gemini Vision request failed: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
        }
