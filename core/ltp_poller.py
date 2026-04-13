"""
LTP Poller - Polls Angel One REST API every 30s for PENDING/OPEN trades.
This is the fallback when WebSocket feed can't find a token or isn't running.
Also resolves symbol → Angel One token using the instrument master.
"""

import re
import threading
import time
from datetime import datetime
from loguru import logger


# ── Symbol Resolver ──────────────────────────────────────────────────────────

def _parse_expiry_dt(exp: str):
    try:
        return datetime.strptime(exp, "%d%b%Y")
    except Exception:
        return datetime.max


def resolve_token(symbol: str, exchange: str) -> tuple[str, str] | None:
    """
    Resolve a signal symbol like 'NIFTY24000CE' to (token, full_symbol).
    Searches the instrument master for the nearest expiry match.
    Returns None if not found.
    """
    try:
        from api.option_chain import _master_cache, load_master, MASTER_FILE
        master = _master_cache or (load_master() if MASTER_FILE.exists() else None)
        if not master:
            return None

        sym = symbol.upper()

        # Try exact symbol match first
        for inst in master:
            if inst.get("symbol", "").upper() == sym and inst.get("exch_seg", "") == exchange:
                return inst.get("token"), inst.get("symbol")

        # Try pattern: extract underlying + strike + type (CE/PE/FUT)
        m = re.match(r'^([A-Z&]+?)(\d+)(CE|PE|FUT)?$', sym)
        if not m:
            return None

        underlying = m.group(1)
        strike_raw = m.group(2)
        opt_type = m.group(3) or ""
        # Angel One stores strike * 100
        strike_val = float(strike_raw) * 100

        candidates = []
        for inst in master:
            inst_sym = inst.get("symbol", "").upper()
            inst_exch = inst.get("exch_seg", "")
            inst_type = inst.get("instrumenttype", "")
            inst_name = inst.get("name", "").upper()
            inst_strike = float(inst.get("strike", 0))

            if (inst_exch == exchange and
                    inst_name == underlying and
                    inst_type in ("OPTIDX", "OPTSTK", "OPTFUT", "FUTSTK", "FUTIDX") and
                    abs(inst_strike - strike_val) < 1.0 and
                    inst_sym.endswith(opt_type)):
                candidates.append(inst)

        if not candidates:
            # Fuzzy: try name startswith
            for inst in master:
                inst_exch = inst.get("exch_seg", "")
                inst_type = inst.get("instrumenttype", "")
                inst_name = inst.get("name", "").upper()
                inst_strike = float(inst.get("strike", 0))
                inst_sym = inst.get("symbol", "").upper()
                if (inst_exch == exchange and
                        inst_name.startswith(underlying[:6]) and
                        inst_type in ("OPTIDX", "OPTSTK") and
                        abs(inst_strike - strike_val) < 1.0 and
                        inst_sym.endswith(opt_type)):
                    candidates.append(inst)

        if not candidates:
            return None

        # Pick nearest expiry
        candidates.sort(key=lambda x: _parse_expiry_dt(x.get("expiry", "")))
        best = candidates[0]
        return best.get("token"), best.get("symbol")

    except Exception as e:
        logger.error(f"resolve_token error for {symbol}: {e}")
        return None


def fetch_ltp_rest(token: str, exchange: str, symbol: str) -> float | None:
    """Fetch LTP via Angel One REST (not WebSocket)"""
    try:
        from data.angel_api import angel_api
        if not angel_api.is_connected() or not token:
            return None
        result = angel_api.api.ltpData(exchange, symbol, token)
        if result and result.get("status"):
            return float(result["data"].get("ltp", 0)) or None
        return None
    except Exception as e:
        logger.debug(f"LTP fetch error [{symbol}]: {e}")
        return None


# ── Poller ───────────────────────────────────────────────────────────────────

class LTPPoller:
    """
    Background thread: every `interval` seconds, fetches LTP for all
    PENDING/OPEN trades and pushes updates to the engine.
    Also populates _symbol_token_map so WebSocket can subscribe.
    """

    def __init__(self, interval: int = 30):
        self.interval = interval
        self._running = False
        self._thread: threading.Thread | None = None
        # token cache: {symbol → (token, full_angel_symbol)}
        self._token_cache: dict[str, tuple[str, str]] = {}
        # ltp cache: {trade_id → ltp} (for API display)
        self.ltp_cache: dict[int, float] = {}

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="LTPPoller")
        self._thread.start()
        logger.info(f"LTP Poller started (interval={self.interval}s)")

    def stop(self):
        self._running = False

    def get_ltp(self, trade_id: int) -> float | None:
        return self.ltp_cache.get(trade_id)

    def _run(self):
        while self._running:
            try:
                self._poll_all()
            except Exception as e:
                logger.error(f"Poller error: {e}")
            time.sleep(self.interval)

    def _poll_all(self):
        from database.db import get_session
        from database.models import Trade, TradeStatus
        from core.engine import engine

        db = get_session()
        try:
            trades = db.query(Trade).filter(
                Trade.status.in_([TradeStatus.PENDING, TradeStatus.OPEN])
            ).all()

            if not trades:
                return

            logger.debug(f"Poller checking {len(trades)} trades")

            for trade in trades:
                ltp = self._get_ltp_for_trade(trade)
                if ltp is None:
                    continue

                self.ltp_cache[trade.id] = ltp

                # Push to engine for entry/exit checking
                engine.process_ltp(trade.id, ltp)

        finally:
            db.close()

    def _get_ltp_for_trade(self, trade) -> float | None:
        from data.market_feed import market_feed
        from core.engine import engine

        symbol = trade.symbol
        exchange = trade.exchange

        # 1. Try WebSocket feed (fastest, already subscribed)
        token = engine._symbol_token_map.get(symbol)
        if token:
            ltp = market_feed.get_ltp(token)
            if ltp:
                return ltp

        # 2. Resolve token from instrument master
        if symbol not in self._token_cache:
            result = resolve_token(symbol, exchange)
            if result:
                tok, full_sym = result
                self._token_cache[symbol] = (tok, full_sym)
                # Register in engine and subscribe to WebSocket
                engine._symbol_token_map[symbol] = tok
                market_feed.subscribe(tok, symbol, exchange)
                if not market_feed._running:
                    market_feed.start()
                logger.info(f"Resolved {symbol} → token={tok} ({full_sym})")
            else:
                self._token_cache[symbol] = (None, None)
                logger.warning(f"Could not resolve token for {symbol} ({exchange})")

        cached = self._token_cache.get(symbol, (None, None))
        token, full_sym = cached

        if not token:
            return None

        # 3. REST fallback LTP
        return fetch_ltp_rest(token, exchange, full_sym or symbol)


# Singleton
ltp_poller = LTPPoller(interval=30)
