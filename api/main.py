import os
import shutil
from datetime import datetime
from pathlib import Path
import pytz
from fastapi import FastAPI, File, UploadFile, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.requests import Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database.db import get_db, init_db
from database.models import Trade, TradeStatus, DailyReport
from parsers.signal_parser import signal_parser
from core.engine import engine
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")

app = FastAPI(title="Paper Trading System", version="1.0.0")

from api.option_chain import router as oc_router
app.include_router(oc_router)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

templates = Jinja2Templates(directory="dashboard/templates")
app.mount("/static", StaticFiles(directory="dashboard/static"), name="static")


# ─── Pydantic schemas ───────────────────────────────────────────────────────

class TradeSignalText(BaseModel):
    text: str
    lot_size: int = 1
    trailing_sl_points: float | None = None
    trailing_method: str = "sl_distance"  # fixed | percent | sl_distance


class ManualTrade(BaseModel):
    symbol: str
    exchange: str
    instrument_type: str
    action: str
    entry_price: float
    stop_loss: float = 0.0       # optional - 0 means no SL
    targets: list[float] = []
    quantity: int
    lot_size: int = 1
    trade_type: str = "INTRADAY"
    trailing_sl_points: float | None = None


class TradeUpdate(BaseModel):
    entry_price: float | None = None
    stop_loss: float | None = None
    trailing_sl: float | None = None
    trailing_sl_points: float | None = None
    target1: float | None = None
    target2: float | None = None
    target3: float | None = None


# ─── Routes ─────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    trades = db.query(Trade).order_by(Trade.created_at.desc()).limit(50).all()
    open_trades = [t for t in trades if t.status in [TradeStatus.OPEN, TradeStatus.PENDING]]
    closed_trades = [t for t in trades if t.status == TradeStatus.CLOSED]
    total_pnl = sum(t.gross_pnl or 0 for t in closed_trades)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "open_trades": open_trades,
        "closed_trades": closed_trades[:20],
        "total_pnl": total_pnl,
        "total_trades": len(closed_trades),
        "winning_trades": sum(1 for t in closed_trades if (t.gross_pnl or 0) > 0),
    })


@app.post("/api/signal/image")
async def upload_signal_image(
    file: UploadFile = File(...),
    lot_size: int = 1,
    trailing_sl_points: float = None,
    trailing_method: str = "sl_distance",
):
    """Upload a Telegram screenshot to parse and create a paper trade"""
    # Save uploaded image
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = Path(file.filename).suffix or ".jpg"
    save_path = UPLOAD_DIR / f"signal_{timestamp}{ext}"

    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Parse signal from image
    signal = signal_parser.parse_image(str(save_path))
    if not signal:
        raise HTTPException(status_code=422, detail="Could not parse trade signal from image")

    # Create paper trade
    trade = engine.add_trade(
        signal,
        lot_size=lot_size,
        trailing_sl_points=trailing_sl_points,
        trailing_method=trailing_method,
    )
    if not trade:
        raise HTTPException(status_code=500, detail="Failed to create trade")

    return {
        "success": True,
        "parsed_signal": signal,
        "trade_id": trade.id,
        "message": f"Trade #{trade.id} created: {signal['action']} {signal['symbol']} @ {signal['entry_price']}",
    }


@app.post("/api/signal/text")
async def signal_from_text(payload: TradeSignalText):
    """Parse a text signal and create a paper trade"""
    signal = signal_parser.parse_text(payload.text)
    if not signal:
        raise HTTPException(status_code=422, detail="Could not parse signal from text")

    trade = engine.add_trade(
        signal,
        lot_size=payload.lot_size,
        trailing_sl_points=payload.trailing_sl_points,
        trailing_method=payload.trailing_method,
    )
    if not trade:
        raise HTTPException(status_code=500, detail="Failed to create trade")

    return {"success": True, "parsed_signal": signal, "trade_id": trade.id}


@app.post("/api/trade/manual")
async def create_manual_trade(payload: ManualTrade):
    """Create a trade manually without image"""
    signal = {
        "action": payload.action,
        "symbol": payload.symbol,
        "exchange": payload.exchange,
        "instrument_type": payload.instrument_type,
        "entry_price": payload.entry_price,
        "entry_type": "LIMIT",
        "stop_loss": payload.stop_loss,
        "targets": payload.targets,
        "quantity": payload.quantity,
        "trade_type": payload.trade_type,
    }
    trade = engine.add_trade(
        signal,
        lot_size=payload.lot_size,
        trailing_sl_points=payload.trailing_sl_points,
    )
    if not trade:
        raise HTTPException(status_code=500, detail="Failed to create trade")
    return {"success": True, "trade_id": trade.id}


@app.get("/api/trades")
async def get_trades(
    status: str = None,
    limit: int = 50,
    db: Session = Depends(get_db)
):
    query = db.query(Trade)
    if status:
        query = query.filter(Trade.status == status)
    trades = query.order_by(Trade.created_at.desc()).limit(limit).all()
    return [_trade_dict(t) for t in trades]


@app.get("/api/trades/open")
async def get_open_trades(db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(
        Trade.status.in_([TradeStatus.OPEN, TradeStatus.PENDING])
    ).all()
    return [_trade_dict(t) for t in trades]


@app.get("/api/trades/open-ltp")
async def get_open_trades_with_ltp(db: Session = Depends(get_db)):
    """Open trades with current LTP from poller cache"""
    from core.ltp_poller import ltp_poller
    trades = db.query(Trade).filter(
        Trade.status.in_([TradeStatus.OPEN, TradeStatus.PENDING])
    ).all()
    result = []
    for t in trades:
        d = _trade_dict(t)
        d["ltp"] = ltp_poller.get_ltp(t.id)
        result.append(d)
    return result


@app.get("/api/trades/{trade_id}")
async def get_trade(trade_id: int, db: Session = Depends(get_db)):
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return _trade_dict(trade)


@app.patch("/api/trades/{trade_id}")
async def update_trade(trade_id: int, payload: TradeUpdate, db: Session = Depends(get_db)):
    """Modify any field of a PENDING or OPEN trade"""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status == TradeStatus.CLOSED:
        raise HTTPException(status_code=400, detail="Cannot modify a closed trade")
    if payload.entry_price is not None:
        trade.entry_price = payload.entry_price
    if payload.stop_loss is not None:
        trade.stop_loss = payload.stop_loss
        if trade.trailing_sl is None or trade.trailing_sl == trade.stop_loss:
            trade.trailing_sl = payload.stop_loss
    if payload.trailing_sl is not None:
        trade.trailing_sl = payload.trailing_sl
    if payload.trailing_sl_points is not None:
        trade.trailing_sl_points = payload.trailing_sl_points
    if payload.target1 is not None:
        trade.target1 = payload.target1
    if payload.target2 is not None:
        trade.target2 = payload.target2
    if payload.target3 is not None:
        trade.target3 = payload.target3
    db.commit()
    return {"success": True, "trade_id": trade_id}


@app.delete("/api/trades/{trade_id}")
async def cancel_trade(trade_id: int, db: Session = Depends(get_db)):
    """Cancel a PENDING trade or force-close an OPEN trade at last LTP"""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    if trade.status == TradeStatus.OPEN:
        # Force close at last known LTP
        from core.ltp_poller import ltp_poller
        ltp = ltp_poller.get_ltp(trade.id) or trade.entry_price
        trade.status = TradeStatus.CLOSED
        trade.exit_price = ltp
        trade.exit_reason = "MANUAL_CLOSE"
        trade.closed_at = datetime.now()
        mult = 1 if trade.action == "BUY" else -1
        trade.gross_pnl = mult * (ltp - trade.entry_price) * trade.quantity
    else:
        trade.status = TradeStatus.CANCELLED

    db.commit()
    return {"success": True}


@app.get("/api/portfolio/summary")
async def portfolio_summary(db: Session = Depends(get_db)):
    from config.settings import settings
    from core.ltp_poller import ltp_poller

    closed = db.query(Trade).filter(Trade.status == TradeStatus.CLOSED).all()
    open_trades = db.query(Trade).filter(
        Trade.status.in_([TradeStatus.OPEN, TradeStatus.PENDING])
    ).all()

    realized_pnl = sum(t.gross_pnl or 0 for t in closed)

    # Compute unrealized P&L from cached LTPs
    unrealized_pnl = 0.0
    for t in open_trades:
        ltp = ltp_poller.get_ltp(t.id)
        if ltp:
            mult = 1 if t.action == "BUY" else -1
            unrealized_pnl += mult * (ltp - t.entry_price) * t.quantity

    total_pnl = realized_pnl + unrealized_pnl
    winners = sum(1 for t in closed if (t.gross_pnl or 0) > 0)

    return {
        "initial_capital": settings.INITIAL_CAPITAL,
        "current_capital": round(settings.INITIAL_CAPITAL + total_pnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "total_trades": len(closed),
        "open_trades": len(open_trades),
        "winning_trades": winners,
        "losing_trades": len(closed) - winners,
        "win_rate": round(winners / len(closed) * 100, 1) if closed else 0,
    }


@app.post("/api/close-intraday")
async def close_intraday():
    """Manually trigger intraday close for all open positions"""
    engine.close_all_intraday()
    return {"success": True, "message": "All intraday positions closed"}


@app.get("/api/status")
async def system_status():
    """Live status of all system components"""
    from data.angel_api import angel_api
    from data.market_feed import market_feed
    from notifications.telegram_bot import telegram_bot
    from scheduler.market_sessions import is_market_open
    from database.db import engine as db_engine

    # DB check
    db_ok = False
    try:
        from sqlalchemy import text
        with db_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    now_ist = datetime.now(IST)
    return {
        "angel_one": {
            "connected": angel_api.is_connected(),
            "client_id": angel_api.api.userId if angel_api.is_connected() and angel_api.api else None,
        },
        "market_feed": {
            "running": market_feed._running,
            "subscriptions": len(market_feed._subscriptions),
            # standby = no trades yet (normal), error = had trades but feed died
            "state": "active" if market_feed._running else (
                "standby" if len(market_feed._subscriptions) == 0 else "error"
            ),
        },
        "database": {"ok": db_ok},
        "telegram": {"configured": telegram_bot.is_configured()},
        "market": {
            "nse_open": is_market_open("NSE"),
            "mcx_open": is_market_open("MCX"),
        },
        "server_time_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
    }


def _trade_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "symbol": t.symbol,
        "exchange": t.exchange,
        "instrument_type": t.instrument_type,
        "action": t.action,
        "trade_type": t.trade_type,
        "status": t.status,
        "entry_price": t.entry_price,
        "entry_type": t.entry_type,
        "quantity": t.quantity,
        "stop_loss": t.stop_loss,
        "trailing_sl": t.trailing_sl,
        "trailing_sl_points": t.trailing_sl_points,
        "target1": t.target1,
        "target2": t.target2,
        "target3": t.target3,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "gross_pnl": t.gross_pnl,
        "signal_source": t.signal_source,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "entry_triggered_at": t.entry_triggered_at.isoformat() if t.entry_triggered_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }
