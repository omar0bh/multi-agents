from fastapi import HTTPException

from app.llm import FREE_TIER_EXTERNAL_MARKER, redact_secrets


def http_error_from_exception(exc: Exception) -> HTTPException:
    message = redact_secrets(str(exc))
    lowered = message.lower()

    if isinstance(exc, RuntimeError) and "missing opencode_zen_api_key" in lowered:
        return HTTPException(
            status_code=503,
            detail="LLM is not configured. Set OPENCODE_ZEN_API_KEY in .env.",
        )

    if isinstance(exc, RuntimeError) and "unsupported llm_model" in lowered:
        return HTTPException(status_code=503, detail=message)

    if FREE_TIER_EXTERNAL_MARKER in lowered:
        return HTTPException(
            status_code=403,
            detail=(
                "Zen free-tier models cannot be used from this external app. "
                "Add pay-as-you-go balance at https://opencode.ai/zen or use "
                "OpenCode directly."
            ),
        )

    if any(
        token in lowered
        for token in ("authentication failed", "unauthorized", "invalid api key")
    ):
        return HTTPException(
            status_code=502,
            detail="LLM authentication failed. Check your Zen API key.",
        )

    if "rate limit" in lowered:
        return HTTPException(
            status_code=429,
            detail="LLM rate limit exceeded. Try again later.",
        )

    if "timed out" in lowered or "timeout" in lowered:
        return HTTPException(
            status_code=504,
            detail="Task timed out before the LLM finished.",
        )

    if "web search failed" in lowered:
        return HTTPException(status_code=502, detail=message)

    return HTTPException(status_code=502, detail=message)
