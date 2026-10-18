import re
from urllib.parse import urlparse

import dspy
import httpx

from app.config import (
    ALLOWED_LLM_MODELS,
    GROQ_API_KEY,
    LLM_API_BASE,
    LLM_MODEL,
    OPENCODE_ZEN_API_KEY,
)

_configured = False

_SECRET_PATTERNS = (
    re.compile(r"gsk_[A-Za-z0-9_\-]+"),
    re.compile(r"grok_[A-Za-z0-9_\-]+"),
    re.compile(r"oc_sk_[A-Za-z0-9_\-]+"),
    re.compile(r"sk-[A-Za-z0-9_\-]+"),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
)


def redact_secrets(text: str) -> str:
    if not text:
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _api_base_host() -> str:
    if LLM_MODEL.startswith("groq/"):
        return "api.groq.com"
    parsed = urlparse(LLM_API_BASE)
    return parsed.netloc or LLM_API_BASE


def configure_dspy():
    global _configured

    if _configured:
        return

    if LLM_MODEL.startswith("groq/"):
        if not GROQ_API_KEY:
            raise RuntimeError(
                "Missing GROQ_API_KEY in .env. Get a free key at https://console.groq.com/keys"
            )
        lm = dspy.LM(
            model=LLM_MODEL,
            api_key=GROQ_API_KEY,
            temperature=0.2,
            max_tokens=2048,
            num_retries=3,
        )
    else:
        if not OPENCODE_ZEN_API_KEY:
            raise RuntimeError(
                "Missing OPENCODE_ZEN_API_KEY. "
                "Add your key to the .env file."
            )
        lm = dspy.LM(
            model=LLM_MODEL,
            api_key=OPENCODE_ZEN_API_KEY,
            api_base=LLM_API_BASE,
            temperature=0.2,
            max_tokens=2048,
            engine="litellm",
            num_retries=3,
        )

    dspy.configure(lm=lm)
    _configured = True


FREE_TIER_EXTERNAL_MARKER = (
    "free tier can only be used from within opencode"
)


class _PingSignature(dspy.Signature):
    """Reply briefly to the user message."""

    message: str = dspy.InputField()
    reply: str = dspy.OutputField()


def verify_zen_api_key() -> dict:
    """Confirm the Zen API key can reach the provider (no completion tokens)."""
    if not OPENCODE_ZEN_API_KEY:
        raise RuntimeError(
            "Missing OPENCODE_ZEN_API_KEY. "
            "Add your key to the .env file."
        )

    url = f"{LLM_API_BASE.rstrip('/')}/models"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {OPENCODE_ZEN_API_KEY}"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Could not reach Zen API at {_api_base_host()}: "
            f"{redact_secrets(str(exc))}"
        ) from exc

    if response.status_code in (401, 403):
        raise RuntimeError(
            "Authentication failed (check OPENCODE_ZEN_API_KEY and Zen billing)."
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Zen API returned HTTP {response.status_code}."
        )

    payload = response.json()
    model_ids = [
        item.get("id", "")
        for item in payload.get("data", [])
        if isinstance(item, dict)
    ]
    configured_tail = LLM_MODEL.split("/", 1)[-1]
    model_listed = configured_tail in model_ids

    return {
        "auth_ok": True,
        "api_base_host": _api_base_host(),
        "model": LLM_MODEL,
        "model_listed": model_listed,
    }


def verify_llm_connection(
    prompt: str = "Reply with exactly: OK",
) -> dict:
    """Run a minimal completion against the configured provider."""
    configure_dspy()

    try:
        result = dspy.Predict(_PingSignature)(message=prompt)
        sample = (result.reply or "").strip()
    except Exception as exc:
        message = redact_secrets(str(exc))
        lowered = message.lower()
        if FREE_TIER_EXTERNAL_MARKER in lowered:
            auth = verify_zen_api_key()
            raise RuntimeError(
                "Zen free-tier models (including Big Pickle) cannot be called "
                "from external apps like this FastAPI service. Your API key "
                "is valid (models endpoint OK). Add pay-as-you-go balance at "
                "https://opencode.ai/zen or run agents inside the OpenCode app."
                f" auth_ok={auth['auth_ok']} model_listed={auth['model_listed']}"
            ) from exc
        if any(
            token in lowered
            for token in (
                "authentication",
                "unauthorized",
                "invalid api key",
                "401",
                "403",
            )
        ):
            key_name = "GROQ_API_KEY" if LLM_MODEL.startswith("groq/") else "OPENCODE_ZEN_API_KEY"
            raise RuntimeError(
                f"Authentication failed (check {key_name} in .env)."
            ) from exc
        if "rate limit" in lowered or "429" in lowered:
            raise RuntimeError(
                "Rate limit exceeded. Retry later or reduce request frequency."
            ) from exc
        raise RuntimeError(
            f"LLM request failed: {message}"
        ) from exc

    return {
        "ok": True,
        "auth_ok": True,
        "completion_ok": True,
        "model": LLM_MODEL,
        "api_base_host": _api_base_host(),
        "sample": sample[:500],
    }
