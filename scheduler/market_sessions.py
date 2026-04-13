"""
Market Session Manager
Handles different market timings for NSE, BSE, MCX instruments.
"""

import pytz
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from config.settings import settings
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")


def get_exchange_for_symbol(symbol: str) -> str:
    """Determine exchange from symbol name"""
    symbol_upper = symbol.upper()
    mcx_symbols = settings.MCX_NON_AGRI_SYMBOLS + ["GOLDM", "SILVERM", "CRUDEOILM"]
    if any(s in symbol_upper for s in mcx_symbols):
        return "MCX"
    if any(s in symbol_upper for s in ["NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]):
        return "NFO"
    return "NSE"


def get_session_close_time(exchange: str, symbol: str = "") -> str:
    """Get market close time for an instrument"""
    symbol_upper = symbol.upper()
    if exchange == "MCX":
        if any(s in symbol_upper for s in settings.MCX_NON_AGRI_SYMBOLS):
            return "23:30"
        return "17:00"  # Agri
    return "15:30"  # NSE/BSE


def is_market_open(exchange: str = "NSE", symbol: str = "") -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False

    current_time = now.strftime("%H:%M")

    if exchange == "MCX":
        open_time = "09:00"
        symbol_upper = symbol.upper()
        if any(s in symbol_upper for s in settings.MCX_NON_AGRI_SYMBOLS):
            close_time = "23:30"
        else:
            close_time = "17:00"
    else:
        open_time = "09:15"
        close_time = "15:30"

    return open_time <= current_time <= close_time


class MarketScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone=IST)
        self._setup_jobs()

    def _setup_jobs(self):
        # NSE/BSE open
        self.scheduler.add_job(
            self._on_nse_open, "cron",
            day_of_week="mon-fri", hour=9, minute=15,
            id="nse_open"
        )
        # NSE/BSE close (intraday auto-close)
        self.scheduler.add_job(
            self._on_nse_close, "cron",
            day_of_week="mon-fri", hour=15, minute=25,  # 5 min before close
            id="nse_close"
        )
        # MCX non-agri close
        self.scheduler.add_job(
            self._on_mcx_close, "cron",
            day_of_week="mon-fri", hour=23, minute=25,
            id="mcx_close"
        )
        # Daily report
        self.scheduler.add_job(
            self._daily_report, "cron",
            day_of_week="mon-fri", hour=23, minute=45,
            id="daily_report"
        )
        # Morning login / reconnect
        self.scheduler.add_job(
            self._morning_connect, "cron",
            day_of_week="mon-fri", hour=9, minute=0,
            id="morning_connect"
        )

    def _morning_connect(self):
        from data.angel_api import angel_api
        from data.market_feed import market_feed
        logger.info("Morning connect: logging in to Angel One...")
        if angel_api.connect():
            market_feed.start()

    def _on_nse_open(self):
        logger.info("NSE market OPEN 9:15 AM")
        from notifications.telegram_bot import telegram_bot
        telegram_bot.send("Market OPEN 9:15 AM | Paper trading active")

    def _on_nse_close(self):
        logger.info("NSE close approaching - closing intraday positions")
        from core.engine import engine
        engine.close_all_intraday()
        from notifications.telegram_bot import telegram_bot
        telegram_bot.send("NSE market CLOSE 3:30 PM | All intraday positions closed")

    def _on_mcx_close(self):
        logger.info("MCX close approaching")
        from notifications.telegram_bot import telegram_bot
        telegram_bot.send("MCX market CLOSE 11:30 PM")

    def _daily_report(self):
        from database.db import get_session
        from database.models import Trade, TradeStatus, DailyReport
        logger.info("Generating daily report...")
        db = get_session()
        try:
            today = datetime.now(IST).strftime("%Y-%m-%d")
            trades = db.query(Trade).filter(
                Trade.closed_at >= today,
                Trade.status == TradeStatus.CLOSED
            ).all()

            total = len(trades)
            winning = sum(1 for t in trades if t.gross_pnl and t.gross_pnl > 0)
            losing = sum(1 for t in trades if t.gross_pnl and t.gross_pnl <= 0)
            total_pnl = sum(t.gross_pnl or 0 for t in trades)
            win_rate = (winning / total * 100) if total > 0 else 0

            report_text = (
                f"\U0001F4CA Daily Report - {today}\n"
                f"Total Trades: {total}\n"
                f"Winning: {winning} | Losing: {losing}\n"
                f"Win Rate: {win_rate:.1f}%\n"
                f"Total P&L: \u20b9{total_pnl:,.2f}"
            )
            logger.info(report_text)

            from notifications.telegram_bot import telegram_bot
            telegram_bot.send(report_text)
        finally:
            db.close()

    def start(self):
        self.scheduler.start()
        logger.info("Market scheduler started")

    def stop(self):
        self.scheduler.shutdown()


market_scheduler = MarketScheduler()
