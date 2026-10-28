"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# On Windows, ensure ProactorEventLoop is used so Playwright can spawn subprocesses
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import dashboard, run, telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("rpa.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    log.info("Running Event Loop type: %s", type(loop).__name__)
    log.info("Starting RPA 2.0 MVP on %s:%s", settings.host, settings.port)
    log.info("Active LLM Provider: %s | Model: %s", settings.llm_provider, settings.llm_model)
    
    poll_task = None
    if settings.telegram_enabled and settings.telegram_bot_token:
        log.info("Starting Telegram Bot poller background task...")
        poll_task = asyncio.create_task(telegram.start_polling())

    yield

    if poll_task:
        poll_task.cancel()
    log.info("Shutting down.")


app = FastAPI(title="RPA 2.0 — Cross-Platform Web Action Agent", lifespan=lifespan)

# Static demo targets (served at /demo/*)
app.mount("/demo", StaticFiles(directory="demo_targets", html=True), name="demo")

# Routers
app.include_router(dashboard.router)
app.include_router(run.router)
app.include_router(telegram.router)


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "telegram_enabled": bool(settings.telegram_enabled and settings.telegram_bot_token),
        "demo_urls": settings.demo_urls,
    }
