import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Query

from app.api.routes import router
from app.bridge.routes import router as bridge_router
from app.config import APP_NAME, LLM_MODEL, TELEGRAM_BOT_TOKEN
from app.integrations.telegram_bot import TelegramEmployeeBot
from app.llm import verify_llm_connection, verify_zen_api_key
from app.observability.logging import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_task = None
    if TELEGRAM_BOT_TOKEN:
        bot = TelegramEmployeeBot()
        logger.info("Initializing Telegram Employee Bot background service...")
        bot_task = asyncio.create_task(bot.start_polling())
    yield
    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title=APP_NAME,
    version="0.1.0",
    description="DSPy multi-agent platform",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(bridge_router)


@app.get("/")
def root():
    return {
        "app": APP_NAME,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health(check_llm: bool = Query(default=False)):
    payload = {
        "status": "ok",
        "app": APP_NAME,
        "llm_model": LLM_MODEL,
    }

    if not check_llm:
        payload["llm"] = "skipped"
        return payload

    try:
        if not LLM_MODEL.startswith("groq/"):
            verify_zen_api_key()
        verify_llm_connection()
    except RuntimeError as exc:
        logger.warning("LLM health check failed: %s", str(exc)[:200])
        payload["status"] = "degraded"
        payload["llm"] = "error"
        payload["llm_detail"] = str(exc)[:500]
        return payload

    payload["llm"] = "ok"
    return payload
