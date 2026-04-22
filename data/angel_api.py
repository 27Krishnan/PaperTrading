import pyotp
import time
from SmartApi import SmartConnect
from config.settings import settings
from loguru import logger


class AngelOneAPI:
    def __init__(self):
        self.api = None
        self.auth_token = None
        self.feed_token = None
        self._connected = False
        self._monitoring = False
        self._heartbeat_failures = 0

    def connect(self) -> bool:
        try:
            self.api = SmartConnect(api_key=settings.ANGEL_API_KEY)
            totp = pyotp.TOTP(settings.ANGEL_TOTP_SECRET).now()
            data = self.api.generateSession(
                settings.ANGEL_CLIENT_ID, settings.ANGEL_PASSWORD, totp
            )
            if data["status"]:
                self.auth_token = data["data"]["jwtToken"]
                self.feed_token = self.api.getfeedToken()
                self._connected = True
                self._heartbeat_failures = 0
                logger.info(f"Angel One connected | Client: {settings.ANGEL_CLIENT_ID}")
                self.start_heartbeat()
                return True
            else:
                logger.error(f"Angel One login failed: {data['message']}")
                return False
        except Exception as e:
            logger.error(f"Angel One connection error: {e}")
            return False

    def start_heartbeat(self):
        """Starts a background thread to check session health every 60s"""
        if self._monitoring:
            return
        self._monitoring = True
        import threading

        t = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="AngelHeartbeat"
        )
        t.start()
        logger.info("Angel One heartbeat monitor started")

    def _heartbeat_loop(self):
        while self._monitoring:
            try:
                if self._connected and self.api:
                    # Quick LTP check to verify session is still alive
                    result = self.api.ltpData("NSE", "Nifty 50", "99926000")
                    if result and result.get("status"):
                        self._heartbeat_failures = 0
                    else:
                        self._heartbeat_failures += 1
                        logger.warning(
                            f"Angel heartbeat failed ({self._heartbeat_failures}/3)"
                        )
                    if self._heartbeat_failures >= 3:
                        logger.warning("Angel heartbeat failed repeatedly, attempting reconnect...")
                        self._try_reconnect()
            except Exception as e:
                self._heartbeat_failures += 1
                logger.debug(f"Heartbeat error ({self._heartbeat_failures}/3): {e}")
                if self._heartbeat_failures >= 3:
                    logger.warning("Angel heartbeat errors repeated, attempting reconnect...")
                    self._try_reconnect()
            import time

            time.sleep(60)

    def is_connected(self) -> bool:
        """Returns the cached connection status (instant, non-blocking)"""
        return self._connected

    def _try_reconnect(self) -> bool:
        """Reconnect with exponential backoff"""
        import pyotp

        for attempt in range(3):
            try:
                self.api = SmartConnect(api_key=settings.ANGEL_API_KEY)
                totp = pyotp.TOTP(settings.ANGEL_TOTP_SECRET).now()
                data = self.api.generateSession(
                    settings.ANGEL_CLIENT_ID, settings.ANGEL_PASSWORD, totp
                )
                if data["status"]:
                    self.auth_token = data["data"]["jwtToken"]
                    self.feed_token = self.api.getfeedToken()
                    self._connected = True
                    self._heartbeat_failures = 0
                    logger.info(f"Angel One reconnected on attempt {attempt + 1}")
                    try:
                        from data.market_feed import market_feed

                        if market_feed._subscriptions:
                            market_feed.stop()
                            time.sleep(1)
                            market_feed.start()
                    except Exception as e:
                        logger.debug(f"Market feed restart skipped: {e}")
                    return True
                else:
                    logger.warning(
                        f"Reconnect attempt {attempt + 1} failed: {data.get('message')}"
                    )
            except Exception as e:
                logger.warning(f"Reconnect attempt {attempt + 1} error: {e}")
            import time

            time.sleep(2**attempt)  # 2, 4, 8 seconds
        self._connected = False
        logger.error("All reconnect attempts failed")
        return False

    def get_ltp(self, exchange: str, symbol: str, token: str) -> float | None:
        try:
            data = self.api.ltpData(exchange, symbol, token)
            if data["status"]:
                return float(data["data"]["ltp"])
            return None
        except Exception as e:
            logger.error(f"LTP fetch error for {symbol}: {e}")
            return None

    def get_quote(self, exchange: str, symbol: str, token: str) -> dict | None:
        try:
            data = self.api.getQuote(exchange, symbol, token)
            if data["status"]:
                return data["data"]
            return None
        except Exception as e:
            logger.error(f"Quote fetch error for {symbol}: {e}")
            return None

    def get_candle_data(
        self, token: str, exchange: str, interval: str, from_date: str, to_date: str
    ) -> list | None:
        """
        interval: ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE, TEN_MINUTE,
                  FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY
        from_date / to_date: "YYYY-MM-DD HH:MM"
        """
        try:
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": interval,
                "fromdate": from_date,
                "todate": to_date,
            }
            data = self.api.getCandleData(params)
            if data["status"]:
                return data["data"]
            return None
        except Exception as e:
            logger.error(f"Candle data error for token {token}: {e}")
            return None

    def search_scrip(self, exchange: str, search_text: str) -> list:
        try:
            data = self.api.searchScrip(exchange, search_text)
            if data["status"]:
                return data["data"]
            return []
        except Exception as e:
            logger.error(f"Search scrip error: {e}")
            return []

    def get_token(self, exchange: str, symbol: str) -> str | None:
        """Get instrument token from symbol name"""
        results = self.search_scrip(exchange, symbol)
        if results:
            return results[0].get("symboltoken")
        return None

    def disconnect(self):
        try:
            if self.api:
                self.api.terminateSession(settings.ANGEL_CLIENT_ID)
            self._monitoring = False
            self._connected = False
            logger.info("Angel One disconnected")
        except Exception as e:
            logger.error(f"Disconnect error: {e}")


# Singleton instance
angel_api = AngelOneAPI()
