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

    def connect(self) -> bool:
        try:
            self.api = SmartConnect(api_key=settings.ANGEL_API_KEY)
            totp = pyotp.TOTP(settings.ANGEL_TOTP_SECRET).now()
            data = self.api.generateSession(
                settings.ANGEL_CLIENT_ID,
                settings.ANGEL_PASSWORD,
                totp
            )
            if data["status"]:
                self.auth_token = data["data"]["jwtToken"]
                self.feed_token = self.api.getfeedToken()
                self._connected = True
                logger.info(f"Angel One connected | Client: {settings.ANGEL_CLIENT_ID}")
                return True
            else:
                logger.error(f"Angel One login failed: {data['message']}")
                return False
        except Exception as e:
            logger.error(f"Angel One connection error: {e}")
            return False

    def is_connected(self) -> bool:
        return self._connected

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

    def get_candle_data(self, token: str, exchange: str, interval: str,
                        from_date: str, to_date: str) -> list | None:
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
            self._connected = False
            logger.info("Angel One disconnected")
        except Exception as e:
            logger.error(f"Disconnect error: {e}")


# Singleton instance
angel_api = AngelOneAPI()
