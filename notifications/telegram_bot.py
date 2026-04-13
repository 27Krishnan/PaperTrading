import requests
from config.settings import settings
from loguru import logger


class TelegramBot:
    def __init__(self):
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send(self, message: str) -> bool:
        if not self.token or not self.chat_id:
            logger.debug("Telegram not configured, skipping notification")
            return False
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                return True
            logger.error(f"Telegram send failed: {resp.text}")
            return False
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False

    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)


telegram_bot = TelegramBot()
