from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, Enum
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime
import enum

Base = declarative_base()


class TradeStatus(str, enum.Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    TARGET_HIT = "TARGET_HIT"
    SL_HIT = "SL_HIT"
    TRAILING_SL_HIT = "TRAILING_SL_HIT"


class TradeType(str, enum.Enum):
    INTRADAY = "INTRADAY"
    POSITIONAL = "POSITIONAL"


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(10), nullable=False)          # NSE, BSE, MCX
    instrument_type = Column(String(20), nullable=False)   # EQ, CE, PE, FUT
    action = Column(String(10), nullable=False)            # BUY, SELL
    trade_type = Column(String(20), default=TradeType.INTRADAY)

    # Entry details
    entry_price = Column(Float, nullable=False)
    entry_type = Column(String(20), default="LIMIT")       # LIMIT, ABOVE, BELOW, MARKET
    quantity = Column(Integer, nullable=False)
    lot_size = Column(Integer, default=1)

    # Risk management
    stop_loss = Column(Float, nullable=False)
    target1 = Column(Float, nullable=True)
    target2 = Column(Float, nullable=True)
    target3 = Column(Float, nullable=True)
    risk_amount = Column(Float, nullable=True)

    # Trailing
    trailing_sl = Column(Float, nullable=True)
    trailing_sl_points = Column(Float, nullable=True)
    trailing_profit = Column(Float, nullable=True)
    highest_price = Column(Float, nullable=True)           # For trailing tracking
    lowest_price = Column(Float, nullable=True)            # For short trailing

    # Execution
    status = Column(String(20), default=TradeStatus.PENDING)
    entry_triggered_at = Column(DateTime, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String(50), nullable=True)
    closed_at = Column(DateTime, nullable=True)

    # P&L
    gross_pnl = Column(Float, default=0.0)
    net_pnl = Column(Float, default=0.0)

    # Source
    signal_source = Column(String(100), nullable=True)     # Telegram channel name
    signal_image_path = Column(String(255), nullable=True)
    raw_signal = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Portfolio(Base):
    __tablename__ = "portfolio"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, default=datetime.now)
    total_capital = Column(Float, nullable=False)
    available_capital = Column(Float, nullable=False)
    used_margin = Column(Float, default=0.0)
    open_pnl = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    total_pnl = Column(Float, default=0.0)


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(20), nullable=False)
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)
    gross_pnl = Column(Float, default=0.0)
    net_pnl = Column(Float, default=0.0)
    win_rate = Column(Float, default=0.0)
    starting_capital = Column(Float, nullable=False)
    ending_capital = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class TickData(Base):
    __tablename__ = "tick_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(10), nullable=False)
    token = Column(String(20), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    ltp = Column(Float, nullable=False)
    open = Column(Float, nullable=True)
    high = Column(Float, nullable=True)
    low = Column(Float, nullable=True)
    close = Column(Float, nullable=True)
    volume = Column(Integer, nullable=True)
