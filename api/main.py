import os
import shutil
from datetime import datetime
from pathlib import Path
from collections import defaultdict
import pytz
from fastapi import FastAPI, File, UploadFile, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.requests import Request
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from database.db import get_db, init_db
from database.models import Trade, TradeStatus, DailyReport, Owner, Strategy
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


# ─── Pydantic schemas ─────────────────────────────────────────────────────────


class TradeSignalText(BaseModel):
    text: str
    lot_size: int = 1
    trailing_sl_points: float | None = None
    trailing_method: str = "sl_distance"


class ManualTrade(BaseModel):
    symbol: str
    exchange: str
    instrument_type: str
    action: str
    entry_price: float
    stop_loss: float = 0.0
    targets: list[float] = []
    quantity: int
    lot_size: int = 1
    trade_type: str = "INTRADAY"
    trailing_sl_points: float | None = None
    owner_id: int | None = None
    strategy: str | None = None


class TradeUpdate(BaseModel):
    entry_price: float | None = None
    stop_loss: float | None = None
    trailing_sl: float | None = None
    trailing_sl_points: float | None = None
    target1: float | None = None
    target2: float | None = None
    target3: float | None = None
    owner_id: int | None = None
    strategy: str | None = None


class OwnerCreate(BaseModel):
    name: str
    color: str = "#6c9eff"
    description: str | None = None


class OwnerUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    description: str | None = None


class StrategyCreate(BaseModel):
    name: str
    owner_id: int | None = None
    description: str | None = None


class BasketPayload(BaseModel):
    legs: List[ManualTrade]


# ─── Dashboard ────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    trades = db.query(Trade).order_by(Trade.created_at.desc()).limit(50).all()
    open_trades = [
        t for t in trades if t.status in [TradeStatus.OPEN, TradeStatus.PENDING]
    ]
    closed_trades = [t for t in trades if t.status == TradeStatus.CLOSED]
    total_pnl = sum(t.gross_pnl or 0 for t in closed_trades)
    owners = db.query(Owner).order_by(Owner.name).all()
    strategies = db.query(Strategy).order_by(Strategy.name).all()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "open_trades": open_trades,
            "closed_trades": closed_trades[:20],
            "total_pnl": total_pnl,
            "total_trades": len(closed_trades),
            "winning_trades": sum(1 for t in closed_trades if (t.gross_pnl or 0) > 0),
            "owners": owners,
            "strategies": strategies,
        },
    )


# ─── Signal endpoints ─────────────────────────────────────────────────────────


@app.post("/api/signal/image")
async def upload_signal_image(
    file: UploadFile = File(...),
    lot_size: int = 1,
    trailing_sl_points: float = None,
    trailing_method: str = "sl_distance",
):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = Path(file.filename).suffix or ".jpg"
    save_path = UPLOAD_DIR / f"signal_{timestamp}{ext}"

    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    signal = signal_parser.parse_image(str(save_path))
    if not signal:
        raise HTTPException(
            status_code=422, detail="Could not parse trade signal from image"
        )

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


# ─── Manual / Basket trades ───────────────────────────────────────────────────


@app.post("/api/trade/manual")
async def create_manual_trade(payload: ManualTrade, db: Session = Depends(get_db)):
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

    # Assign owner / strategy if provided
    if payload.owner_id is not None:
        db_trade = db.query(Trade).filter(Trade.id == trade.id).first()
        if db_trade:
            db_trade.owner_id = payload.owner_id
            db_trade.strategy = payload.strategy
            db.commit()

    return {"success": True, "trade_id": trade.id}


@app.post("/api/basket")
async def execute_basket(payload: BasketPayload, db: Session = Depends(get_db)):
    """Execute multiple trade legs atomically. Returns per-leg results."""
    if not payload.legs:
        raise HTTPException(status_code=400, detail="Basket is empty")

    results = []
    for leg in payload.legs:
        signal = {
            "action": leg.action,
            "symbol": leg.symbol,
            "exchange": leg.exchange,
            "instrument_type": leg.instrument_type,
            "entry_price": leg.entry_price,
            "entry_type": "LIMIT",
            "stop_loss": leg.stop_loss,
            "targets": leg.targets,
            "quantity": leg.quantity,
            "trade_type": leg.trade_type,
        }
        try:
            trade = engine.add_trade(
                signal,
                lot_size=leg.lot_size,
                trailing_sl_points=leg.trailing_sl_points,
            )
            if trade and (leg.owner_id is not None or leg.strategy):
                db_trade = db.query(Trade).filter(Trade.id == trade.id).first()
                if db_trade:
                    db_trade.owner_id = leg.owner_id
                    db_trade.strategy = leg.strategy
                    db.commit()

            results.append(
                {
                    "symbol": leg.symbol,
                    "action": leg.action,
                    "success": bool(trade),
                    "trade_id": trade.id if trade else None,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "symbol": leg.symbol,
                    "action": leg.action,
                    "success": False,
                    "error": str(exc),
                }
            )

    total_ok = sum(1 for r in results if r["success"])
    return {"total": len(results), "created": total_ok, "results": results}


# ─── Trade CRUD ───────────────────────────────────────────────────────────────


@app.get("/api/trades")
async def get_trades(
    status: str = None,
    owner_id: int = None,
    strategy: str = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(Trade)
    if status:
        query = query.filter(Trade.status == status)
    if owner_id is not None:
        query = query.filter(Trade.owner_id == owner_id)
    if strategy:
        query = query.filter(Trade.strategy == strategy)
    trades = query.order_by(Trade.created_at.desc()).limit(limit).all()
    return [_trade_dict(t) for t in trades]


@app.get("/api/trades/open")
async def get_open_trades(owner_id: int = None, db: Session = Depends(get_db)):
    query = db.query(Trade).filter(
        Trade.status.in_([TradeStatus.OPEN, TradeStatus.PENDING])
    )
    if owner_id is not None:
        query = query.filter(Trade.owner_id == owner_id)
    return [_trade_dict(t) for t in query.all()]


@app.get("/api/trades/open-ltp")
async def get_open_trades_with_ltp(db: Session = Depends(get_db)):
    from core.ltp_poller import ltp_poller

    trades = (
        db.query(Trade)
        .filter(Trade.status.in_([TradeStatus.OPEN, TradeStatus.PENDING]))
        .all()
    )
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
async def update_trade(
    trade_id: int, payload: TradeUpdate, db: Session = Depends(get_db)
):
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
    if payload.owner_id is not None:
        trade.owner_id = payload.owner_id
    if payload.strategy is not None:
        trade.strategy = payload.strategy
    db.commit()
    return {"success": True, "trade_id": trade_id}


@app.delete("/api/trades/{trade_id}")
async def cancel_trade(trade_id: int, db: Session = Depends(get_db)):
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    if trade.status == TradeStatus.OPEN:
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


# ─── Owner endpoints ──────────────────────────────────────────────────────────


@app.get("/api/owners")
async def list_owners(db: Session = Depends(get_db)):
    owners = db.query(Owner).order_by(Owner.name).all()
    result = []
    for o in owners:
        closed = [t for t in o.trades if t.status == TradeStatus.CLOSED]
        open_t = [
            t for t in o.trades if t.status in [TradeStatus.OPEN, TradeStatus.PENDING]
        ]
        result.append(
            {
                "id": o.id,
                "name": o.name,
                "color": o.color,
                "description": o.description,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "trade_count": len(o.trades),
                "open_count": len(open_t),
                "closed_count": len(closed),
                "realized_pnl": round(sum(t.gross_pnl or 0 for t in closed), 2),
                "win_rate": round(
                    sum(1 for t in closed if (t.gross_pnl or 0) > 0)
                    / len(closed)
                    * 100,
                    1,
                )
                if closed
                else 0,
            }
        )
    return result


@app.post("/api/owners", status_code=201)
async def create_owner(payload: OwnerCreate, db: Session = Depends(get_db)):
    existing = db.query(Owner).filter(Owner.name == payload.name).first()
    if existing:
        raise HTTPException(
            status_code=409, detail="Owner with this name already exists"
        )
    owner = Owner(
        name=payload.name, color=payload.color, description=payload.description
    )
    db.add(owner)
    db.commit()
    db.refresh(owner)
    return {"success": True, "id": owner.id, "name": owner.name, "color": owner.color}


@app.patch("/api/owners/{owner_id}")
async def update_owner(
    owner_id: int, payload: OwnerUpdate, db: Session = Depends(get_db)
):
    owner = db.query(Owner).filter(Owner.id == owner_id).first()
    if not owner:
        raise HTTPException(status_code=404, detail="Owner not found")
    if payload.name is not None:
        owner.name = payload.name
    if payload.color is not None:
        owner.color = payload.color
    if payload.description is not None:
        owner.description = payload.description
    db.commit()
    return {"success": True}


@app.delete("/api/owners/{owner_id}")
async def delete_owner(owner_id: int, db: Session = Depends(get_db)):
    owner = db.query(Owner).filter(Owner.id == owner_id).first()
    if not owner:
        raise HTTPException(status_code=404, detail="Owner not found")
    # Set trades' owner_id to NULL before deleting
    for t in owner.trades:
        t.owner_id = None
    db.delete(owner)
    db.commit()
    return {"success": True}


# ─── Strategy endpoints ───────────────────────────────────────────────────────


@app.get("/api/strategies")
async def list_strategies(owner_id: int = None, db: Session = Depends(get_db)):
    query = db.query(Strategy)
    if owner_id is not None:
        query = query.filter(Strategy.owner_id == owner_id)
    strats = query.order_by(Strategy.name).all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "owner_id": s.owner_id,
            "owner_name": s.owner.name if s.owner else None,
            "owner_color": s.owner.color if s.owner else "#555",
            "description": s.description,
        }
        for s in strats
    ]


@app.post("/api/strategies", status_code=201)
async def create_strategy(payload: StrategyCreate, db: Session = Depends(get_db)):
    if payload.owner_id:
        owner = db.query(Owner).filter(Owner.id == payload.owner_id).first()
        if not owner:
            raise HTTPException(status_code=404, detail="Owner not found")
    strat = Strategy(
        name=payload.name, owner_id=payload.owner_id, description=payload.description
    )
    db.add(strat)
    db.commit()
    db.refresh(strat)
    return {"success": True, "id": strat.id, "name": strat.name}


@app.delete("/api/strategies/{strategy_id}")
async def delete_strategy(strategy_id: int, db: Session = Depends(get_db)):
    strat = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found")
    db.delete(strat)
    db.commit()
    return {"success": True}


# ─── P&L Breakdown ───────────────────────────────────────────────────────────


@app.get("/api/pnl/breakdown")
async def pnl_breakdown(
    owner_id: int = None,
    strategy: str = None,
    year: int = None,
    month: int = None,
    db: Session = Depends(get_db),
):
    """Returns per-trade closed P&L with optional filters, plus monthly summary."""
    query = db.query(Trade).filter(Trade.status == TradeStatus.CLOSED)

    if owner_id is not None:
        query = query.filter(Trade.owner_id == owner_id)
    if strategy:
        query = query.filter(Trade.strategy == strategy)
    if year:
        query = query.filter(
            Trade.closed_at >= datetime(year, 1, 1),
            Trade.closed_at < datetime(year + 1, 1, 1),
        )
    if month and year:
        import calendar

        last_day = calendar.monthrange(year, month)[1]
        query = query.filter(
            Trade.closed_at >= datetime(year, month, 1),
            Trade.closed_at <= datetime(year, month, last_day, 23, 59, 59),
        )

    trades = query.order_by(Trade.closed_at.desc()).all()

    # Monthly buckets {YYYY-MM: { pnl, count, wins }}
    monthly: dict = defaultdict(
        lambda: {"pnl": 0.0, "count": 0, "wins": 0, "losses": 0}
    )
    owner_summary: dict = defaultdict(
        lambda: {"pnl": 0.0, "count": 0, "wins": 0, "name": "", "color": "#555"}
    )
    strategy_summary: dict = defaultdict(lambda: {"pnl": 0.0, "count": 0, "wins": 0})

    trade_rows = []
    for t in trades:
        pnl = t.gross_pnl or 0
        win = pnl > 0
        closed_str = t.closed_at.strftime("%Y-%m") if t.closed_at else "Unknown"
        pts = ((t.exit_price or t.entry_price) - t.entry_price) * (
            1 if t.action == "BUY" else -1
        )

        monthly[closed_str]["pnl"] += pnl
        monthly[closed_str]["count"] += 1
        if win:
            monthly[closed_str]["wins"] += 1
        else:
            monthly[closed_str]["losses"] += 1

        oid = t.owner_id or 0
        owner_summary[oid]["pnl"] += pnl
        owner_summary[oid]["count"] += 1
        if win:
            owner_summary[oid]["wins"] += 1
        if t.owner:
            owner_summary[oid]["name"] = t.owner.name
            owner_summary[oid]["color"] = t.owner.color
        elif oid == 0:
            owner_summary[oid]["name"] = "Unassigned"

        strat_key = t.strategy or "Untagged"
        strategy_summary[strat_key]["pnl"] += pnl
        strategy_summary[strat_key]["count"] += 1
        if win:
            strategy_summary[strat_key]["wins"] += 1

        trade_rows.append(
            {
                "id": t.id,
                "symbol": t.symbol,
                "action": t.action,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "points": round(pts, 2),
                "pnl": round(pnl, 2),
                "exit_reason": t.exit_reason or t.status,
                "strategy": t.strategy or "",
                "owner_id": t.owner_id,
                "owner_name": t.owner.name if t.owner else "—",
                "owner_color": t.owner.color if t.owner else "#555",
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
                "entry_triggered_at": t.entry_triggered_at.isoformat()
                if t.entry_triggered_at
                else None,
            }
        )

    monthly_list = [
        {
            "month": k,
            "pnl": round(v["pnl"], 2),
            "count": v["count"],
            "wins": v["wins"],
            "losses": v["losses"],
            "win_rate": round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0,
        }
        for k, v in sorted(monthly.items(), reverse=True)
    ]

    owner_list = [
        {
            "owner_id": k,
            "name": v["name"],
            "color": v["color"],
            "pnl": round(v["pnl"], 2),
            "count": v["count"],
            "wins": v["wins"],
            "win_rate": round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0,
        }
        for k, v in owner_summary.items()
    ]

    strategy_list = [
        {
            "strategy": k,
            "pnl": round(v["pnl"], 2),
            "count": v["count"],
            "wins": v["wins"],
            "win_rate": round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0,
        }
        for k, v in sorted(strategy_summary.items(), key=lambda x: -x[1]["pnl"])
    ]

    total_pnl = sum(t["pnl"] for t in trade_rows)
    total_wins = sum(1 for t in trade_rows if t["pnl"] > 0)
    best = max((t["pnl"] for t in trade_rows), default=0)
    worst = min((t["pnl"] for t in trade_rows), default=0)

    return {
        "total_pnl": round(total_pnl, 2),
        "total_trades": len(trade_rows),
        "total_wins": total_wins,
        "win_rate": round(total_wins / len(trade_rows) * 100, 1) if trade_rows else 0,
        "best_trade": round(best, 2),
        "worst_trade": round(worst, 2),
        "monthly": monthly_list,
        "by_owner": owner_list,
        "by_strategy": strategy_list,
        "trades": trade_rows,
    }


# ─── Portfolio summary ────────────────────────────────────────────────────────


@app.get("/api/portfolio/summary")
async def portfolio_summary(db: Session = Depends(get_db)):
    from config.settings import settings
    from core.ltp_poller import ltp_poller

    closed = db.query(Trade).filter(Trade.status == TradeStatus.CLOSED).all()
    open_trades = (
        db.query(Trade)
        .filter(Trade.status.in_([TradeStatus.OPEN, TradeStatus.PENDING]))
        .all()
    )

    realized_pnl = sum(t.gross_pnl or 0 for t in closed)

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
    engine.close_all_intraday()
    return {"success": True, "message": "All intraday positions closed"}


@app.get("/api/status")
async def system_status():
    from data.angel_api import angel_api
    from data.market_feed import market_feed
    from notifications.telegram_bot import telegram_bot
    from scheduler.market_sessions import is_market_open
    from database.db import engine as db_engine

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
            "client_id": angel_api.api.userId
            if angel_api.is_connected() and angel_api.api
            else None,
        },
        "market_feed": {
            "running": market_feed._running,
            "subscriptions": len(market_feed._subscriptions),
            "state": "active"
            if market_feed._running
            else ("standby" if len(market_feed._subscriptions) == 0 else "error"),
        },
        "database": {"ok": db_ok},
        "telegram": {"configured": telegram_bot.is_configured()},
        "market": {
            "nse_open": is_market_open("NSE"),
            "mcx_open": is_market_open("MCX"),
        },
        "server_time_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
    }


@app.get("/api/health")
async def health_check():
    """
    Health check endpoint - returns status of all dependencies.
    Use this to diagnose issues with EasyOCR, Angel One, etc.
    """
    from data.angel_api import angel_api
    from data.market_feed import market_feed
    from notifications.telegram_bot import telegram_bot
    from scheduler.market_sessions import is_market_open
    from database.db import engine as db_engine
    import pytz

    IST = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(IST)

    # Check EasyOCR
    ocr_status = {"loaded": False, "error": None}
    try:
        from parsers.signal_parser import _ocr_reader, _get_reader

        if _ocr_reader is not None:
            ocr_status["loaded"] = True
        else:
            # Try to load it
            try:
                _get_reader()
                ocr_status["loaded"] = True
            except Exception as e:
                ocr_status["error"] = str(e)
    except Exception as e:
        ocr_status["error"] = str(e)

    # Check Angel One
    angel_status = {"connected": angel_api.is_connected()}
    if angel_status["connected"]:
        try:
            angel_status["client_id"] = angel_api.api.userId if angel_api.api else None
            angel_status["auth_token_present"] = bool(angel_api.auth_token)
            angel_status["feed_token_present"] = bool(angel_api.feed_token)
        except Exception as e:
            angel_status["error"] = str(e)

    # Check Database
    db_status = {"ok": False}
    try:
        from sqlalchemy import text

        with db_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status["ok"] = True
    except Exception as e:
        db_status["error"] = str(e)

    # Check Market Feed
    feed_status = {
        "running": market_feed._running,
        "subscriptions": len(market_feed._subscriptions),
    }

    # Check Telegram
    telegram_status = {"configured": telegram_bot.is_configured()}

    # Market status
    market_status = {
        "nse_open": is_market_open("NSE"),
        "mcx_open": is_market_open("MCX"),
        "current_time_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
    }

    # Overall health
    all_ok = ocr_status.get("loaded") and db_status["ok"]

    return {
        "healthy": all_ok,
        "timestamp": now_ist.isoformat(),
        "components": {
            "easyocr": ocr_status,
            "angel_one": angel_status,
            "database": db_status,
            "market_feed": feed_status,
            "telegram": telegram_status,
            "market": market_status,
        },
        "recommendations": _get_recommendations(
            ocr_status, angel_status, db_status, market_status
        ),
    }


def _get_recommendations(ocr, angel, db, market) -> list:
    """Generate recommendations based on component status"""
    recs = []

    if not ocr.get("loaded"):
        recs.append(
            "EasyOCR not loaded - image parsing will fail. Install torch, torchvision, easyocr on server."
        )

    if not angel.get("connected"):
        recs.append(
            "Angel One not connected - LTP and option chain won't work. Check credentials in .env"
        )

    if not db.get("ok"):
        recs.append("Database error - check database connectivity")

    if not market.get("nse_open") and not market.get("mcx_open"):
        recs.append("Markets are currently closed - LTP updates will be paused")

    if not recs:
        recs.append("All systems operational")

    return recs


# ─── Helper ───────────────────────────────────────────────────────────────────


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
        "strategy": t.strategy,
        "owner_id": t.owner_id,
        "owner_name": t.owner.name if t.owner else None,
        "owner_color": t.owner.color if t.owner else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "entry_triggered_at": t.entry_triggered_at.isoformat()
        if t.entry_triggered_at
        else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }
