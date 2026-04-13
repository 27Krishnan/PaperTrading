import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Angel One
    ANGEL_CLIENT_ID: str = os.getenv("ANGEL_CLIENT_ID", "")
    ANGEL_PASSWORD: str = os.getenv("ANGEL_PASSWORD", "")
    ANGEL_API_KEY: str = os.getenv("ANGEL_API_KEY", "")
    ANGEL_SECRET_KEY: str = os.getenv("ANGEL_SECRET_KEY", "")
    ANGEL_TOTP_SECRET: str = os.getenv("ANGEL_TOTP_SECRET", "")

    # Anthropic
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # App
    INITIAL_CAPITAL: float = float(os.getenv("INITIAL_CAPITAL", "1000000"))
    APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
    APP_PORT: int = int(os.getenv("APP_PORT", "8000"))
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # Market session timings (IST)
    MARKET_SESSIONS = {
        "NSE_EQ": {"open": "09:15", "close": "15:30"},
        "NSE_FO": {"open": "09:15", "close": "15:30"},
        "BSE_EQ": {"open": "09:15", "close": "15:30"},
        "MCX_AGRI": {"open": "09:00", "close": "17:00"},
        "MCX_NON_AGRI": {"open": "09:00", "close": "23:30"},  # Gold, Silver, Crude
    }

    # Instruments that go till 11:30 PM
    MCX_NON_AGRI_SYMBOLS = ["GOLD", "SILVER", "CRUDE", "CRUDEOIL", "NATURALGAS", "COPPER", "ZINC", "ALUMINIUM", "LEAD", "NICKEL"]


settings = Settings()
