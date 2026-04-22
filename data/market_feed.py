import json
import threading
import time
from datetime import datetime
from typing import Callable
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from data.angel_api import angel_api
from config.settings import settings
from core.utils import get_now_ist
from loguru import logger


class MarketFeed:
    """
    Real-time market data feed using Angel One WebSocket.
    Provides tick-by-tick LTP updates for subscribed instruments.
    """

    def __init__(self):
        self.sws = None
        self._subscriptions: dict[str, dict] = {}  # token -> {symbol, exchange, ltp, ...}
        self._callbacks: list[Callable] = []
        self._running = False
        self._thread = None

    def add_callback(self, fn: Callable):
        """Register a callback to receive tick updates: fn(token, ltp, tick_data)"""
        self._callbacks.append(fn)

    def subscribe(self, token: str, symbol: str, exchange: str):
        self._subscriptions[token] = {"symbol": symbol, "exchange": exchange, "ltp": None}
        logger.info(f"Subscribed: {symbol} ({exchange}) token={token}")

    def unsubscribe(self, token: str):
        self._subscriptions.pop(token, None)

    def start(self):
        if self._running:
            logger.debug("Market feed already running, skipping start")
            return
        if not angel_api.is_connected():
            logger.error("Angel One not connected. Cannot start feed.")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()
        logger.info("Market feed started")

    def stop(self):
        self._running = False
        if self.sws:
            self.sws.close_connection()
        logger.info("Market feed stopped")

    def get_ltp(self, token: str) -> float | None:
        entry = self._subscriptions.get(token)
        if entry:
            return entry["ltp"]
        return None

    def _run_ws(self):
        try:
            correlation_id = "papertrading"
            mode = 3  # SNAP_QUOTE (full data)

            token_list = [
                {"exchangeType": self._exchange_type(info["exchange"]), "tokens": [token]}
                for token, info in self._subscriptions.items()
            ]

            # Don't start WebSocket if no subscriptions yet - wait for first trade
            if not token_list:
                logger.info("No subscriptions yet - WebSocket will connect on first trade")
                self._running = False
                return

            self.sws = SmartWebSocketV2(
                angel_api.auth_token,
                settings.ANGEL_API_KEY,
                settings.ANGEL_CLIENT_ID,
                angel_api.feed_token,
            )

            self.sws.on_open = lambda wsapp: self._on_open(wsapp, correlation_id, mode, token_list)
            self.sws.on_data = self._on_data
            self.sws.on_error = self._on_error
            self.sws.on_close = self._on_close

            self.sws.connect()
        except Exception as e:
            logger.error(f"WebSocket run error: {e}")

    def _on_open(self, wsapp, correlation_id, mode, token_list):
        logger.info("WebSocket connected")
        self.sws.subscribe(correlation_id, mode, token_list)

    def _on_data(self, wsapp, message):
        try:
            token = str(message.get("token", ""))
            ltp = message.get("last_traded_price", 0) / 100.0  # Angel returns paisa

            if token in self._subscriptions:
                self._subscriptions[token]["ltp"] = ltp
                tick = {
                    "token": token,
                    "symbol": self._subscriptions[token]["symbol"],
                    "exchange": self._subscriptions[token]["exchange"],
                    "ltp": ltp,
                    "timestamp": get_now_ist(),
                    "open": message.get("open_price_of_the_day", 0) / 100.0,
                    "high": message.get("high_price_of_the_day", 0) / 100.0,
                    "low": message.get("low_price_of_the_day", 0) / 100.0,
                    "close": message.get("closed_price", 0) / 100.0,
                    "volume": message.get("volume_trade_for_the_day", 0),
                }
                for cb in self._callbacks:
                    try:
                        cb(token, ltp, tick)
                    except Exception as e:
                        logger.error(f"Callback error: {e}")
        except Exception as e:
            logger.error(f"Data processing error: {e}")

    def _on_error(self, wsapp, error):
        logger.error(f"WebSocket error: {error}")

    def _on_close(self, wsapp):
        logger.warning("WebSocket closed")
        if self._running:
            logger.info("Reconnecting in 5 seconds...")
            threading.Thread(target=self._delayed_reconnect, daemon=True).start()

    def _delayed_reconnect(self):
        time.sleep(5)
        if self._running:
            self._run_ws()

    @staticmethod
    def _exchange_type(exchange: str) -> int:
        mapping = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "NCDEX": 7}
        return mapping.get(exchange.upper(), 1)


# Singleton
market_feed = MarketFeed()
