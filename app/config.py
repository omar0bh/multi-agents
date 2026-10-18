import os
from dotenv import load_dotenv

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "DSPy Multi-Agent Platform")
OPENCODE_ZEN_API_KEY = os.getenv("OPENCODE_ZEN_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq" if GROQ_API_KEY else "opencode_zen")

# Default model based on provider
DEFAULT_MODEL = "groq/openai/gpt-oss-120b" if LLM_PROVIDER == "groq" else "openai/big-pickle"
LLM_MODEL = os.getenv("LLM_MODEL", DEFAULT_MODEL)

LLM_API_BASE = os.getenv(
    "LLM_API_BASE",
    "https://opencode.ai/zen/v1"
)
SEARCH_MAX_RESULTS = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "120"))

# Telegram Employee Bot settings
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")

ALLOWED_LLM_MODELS = frozenset(
    {
        "openai/big-pickle",
        "groq/openai/gpt-oss-120b",
        "groq/openai/gpt-oss-20b",
        "groq/qwen/qwen3.8-27b",
        "groq/allam-2-7b",
        "groq/llama-3.3-70b-versatile",
        "groq/llama-3.1-8b-instant",
        "groq/mixtral-8x7b-32768",
    }
)

