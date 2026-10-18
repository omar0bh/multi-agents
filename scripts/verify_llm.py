"""Verify OpenCode Zen / Big Pickle connectivity through DSPy."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm import verify_llm_connection, verify_zen_api_key  # noqa: E402


def main() -> int:
    from app.config import LLM_MODEL
    if not LLM_MODEL.startswith("groq/"):
        try:
            auth = verify_zen_api_key()
            print(
                f"Zen auth: ok=True model={auth['model']} "
                f"listed={auth['model_listed']} host={auth['api_base_host']}"
            )
        except RuntimeError as exc:
            print(f"Zen auth check failed: {exc}", file=sys.stderr)
            return 1

    try:
        result = verify_llm_connection()
    except RuntimeError as exc:
        print(f"LLM completion check failed: {exc}", file=sys.stderr)
        return 2

    print(f"LLM verification: ok={result['ok']} model={result['model']}")
    print(f"Sample: {result['sample']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
