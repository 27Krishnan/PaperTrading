"""
Paper Trading System - Entry Point
Run: uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os
import sys
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from loguru import logger

# Configure logger before anything else
# Force UTF-8 on Windows console to handle emoji in log messages
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf_8"):
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logger.remove()
logger.add(sys.stdout, format="{time:HH:mm:ss} | {level} | {message}", level="INFO")
os.makedirs("logs", exist_ok=True)
logger.add("logs/papertrading.log", rotation="1 day", retention="7 days", level="DEBUG")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ────────────────────────────────────────────────
    from database.db import init_db
    from data.angel_api import angel_api
    from data.market_feed import market_feed
    from scheduler.market_sessions import market_scheduler
    from config.settings import settings
    from loguru import logger

    init_db()
    logger.info("Database ready")

    # Check Angel One credentials and connect
    if settings.ANGEL_CLIENT_ID and settings.ANGEL_API_KEY:
        connected = angel_api.connect()
        if not connected:
            logger.warning("Angel One connection failed - running in offline mode")
        else:
            # Pre-cache instrument master for option chain
            try:
                from api.option_chain import load_master

                logger.info("Loading instrument master...")
                master = load_master()
                logger.info(f"Instrument master cached: {len(master)} instruments")
            except Exception as e:
                logger.warning(f"Could not cache instrument master: {e}")
    else:
        logger.warning("Angel One credentials not configured - running in demo mode")

    # Check EasyOCR availability
    try:
        from parsers.signal_parser import _get_reader

        logger.info("Checking EasyOCR availability...")
        reader = _get_reader()
        logger.info("EasyOCR ready")
    except Exception as e:
        logger.warning(f"EasyOCR not available: {e}. Image parsing will use text only.")

    # Feed starts lazily on first trade subscription
    # Start LTP poller (REST fallback for PENDING/OPEN trades)
    from core.ltp_poller import ltp_poller

    ltp_poller.start()

    market_scheduler.start()
    logger.info("Paper Trading System READY")

    yield  # ── App running ──────────────────────────────────────

    # ── Shutdown ───────────────────────────────────────────────
    from core.ltp_poller import ltp_poller

    ltp_poller.stop()
    market_feed.stop()
    angel_api.disconnect()
    market_scheduler.stop()
    logger.info("Paper Trading System stopped")


# Patch the app to use lifespan
from api.main import app

app.router.lifespan_context = lifespan


if __name__ == "__main__":
    from config.settings import settings

    uvicorn.run(
        "main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=False,
    )
