"""
Paper Trading Engine - Core execution loop.

Flow:
1. Receive trade signal (from image parser or manual input)
2. Create PENDING trade in DB
3. Subscribe to live market feed for that symbol
4. On each tick:
   a. Check if PENDING entry condition is met → set OPEN
   b. Check SL → close with SL_HIT
   c. Check Trailing SL → update TSL, close if triggered
   d. Check Targets → update SL to breakeven/T1, close if final target hit
5. End-of-session → close remaining intraday positions
"""

from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from database.models import Trade, TradeStatus
from database.db import get_session
from data.market_feed import market_feed
from data.angel_api import angel_api
from strategies.trailing_sl import TrailingStopLoss
from strategies.trailing_profit import TrailingProfit
from scheduler.market_sessions import get_exchange_for_symbol
from loguru import logger


class PaperTradingEngine:

    def __init__(self):
        self._active_trades: dict[int, Trade] = {}  # trade_id → Trade
        self._symbol_token_map: dict[str, str] = {}  # symbol → token
        market_feed.add_callback(self._on_tick)

    def add_trade(self, signal: dict, lot_size: int = 1,
                  trailing_sl_points: float = None,
                  trailing_method: str = "sl_distance") -> Trade | None:
        """
        Create a new paper trade from parsed signal.
        trailing_sl_points: if None, uses entry-SL distance as trailing
        """
        db = get_session()
        try:
            entry = signal.get("entry_price")
            sl = signal.get("stop_loss")
            targets = signal.get("targets", [])
            action = signal.get("action", "BUY").upper()

            # Quantity
            qty = signal.get("quantity")
            if not qty:
                from parsers.signal_parser import signal_parser
                qty = signal_parser.calculate_quantity(signal, lot_size)

            # Trailing SL setup – only when SL is provided
            tsl_points = None
            if trailing_sl_points:
                tsl_points = trailing_sl_points
            elif sl:
                tsl_points = TrailingStopLoss.calculate_initial_trailing_points(
                    entry, sl, method=trailing_method
                )

            trade = Trade(
                symbol=signal.get("symbol", ""),
                exchange=signal.get("exchange", "NFO"),
                instrument_type=signal.get("instrument_type", "EQ"),
                action=action,
                trade_type=signal.get("trade_type", "INTRADAY"),
                entry_price=entry,
                entry_type=signal.get("entry_type", "LIMIT"),
                quantity=qty,
                lot_size=lot_size,
                stop_loss=sl or 0.0,
                target1=targets[0] if len(targets) > 0 else None,
                target2=targets[1] if len(targets) > 1 else None,
                target3=targets[2] if len(targets) > 2 else None,
                risk_amount=signal.get("risk_amount"),
                trailing_sl=sl if sl else None,
                trailing_sl_points=tsl_points,
                highest_price=entry if action == "BUY" else None,
                lowest_price=entry if action == "SELL" else None,
                status=TradeStatus.PENDING,
                signal_source=signal.get("source_channel"),
                raw_signal=signal.get("raw_text"),
            )
            db.add(trade)
            db.commit()
            db.refresh(trade)

            # Subscribe to market feed
            self._subscribe_symbol(trade)
            self._active_trades[trade.id] = trade

            logger.info(
                f"Trade #{trade.id} created | {action} {trade.symbol} "
                f"Entry={entry} SL={sl or 'none'} TSL_pts={f'{tsl_points:.2f}' if tsl_points else 'none'} Targets={targets}"
            )
            return trade
        except Exception as e:
            logger.error(f"Add trade error: {e}")
            db.rollback()
            return None
        finally:
            db.close()

    def _subscribe_symbol(self, trade: Trade):
        symbol = trade.symbol
        if symbol not in self._symbol_token_map:
            token = angel_api.get_token(trade.exchange, symbol)
            if token:
                self._symbol_token_map[symbol] = token
                market_feed.subscribe(token, symbol, trade.exchange)
                # Start/restart feed now that we have a subscription
                if not market_feed._running:
                    market_feed.start()
            else:
                logger.warning(f"Could not find token for {symbol} on {trade.exchange}")

    def process_ltp(self, trade_id: int, ltp: float):
        """
        Process a single trade given current LTP.
        Called by WebSocket tick AND by LTPPoller (REST fallback).
        """
        db = get_session()
        try:
            trade = db.query(Trade).filter(Trade.id == trade_id).first()
            if not trade:
                return
            if trade.status == TradeStatus.PENDING:
                self._check_entry(trade, ltp, db)
            elif trade.status == TradeStatus.OPEN:
                close_reason = self._check_exit(trade, ltp, db)
                if close_reason:
                    self._active_trades.pop(trade_id, None)
            db.commit()
        except Exception as e:
            logger.error(f"process_ltp error trade #{trade_id}: {e}")
            db.rollback()
        finally:
            db.close()

    def _on_tick(self, token: str, ltp: float, tick_data: dict):
        """Called on every tick from WebSocket"""
        for trade_id, trade in list(self._active_trades.items()):
            symbol = trade.symbol
            if self._symbol_token_map.get(symbol) != token:
                continue
            self.process_ltp(trade_id, ltp)

    def _check_entry(self, trade: Trade, ltp: float, db: Session):
        triggered = TrailingProfit.check_entry_trigger(
            trade.action, ltp, trade.entry_price, trade.entry_type
        )
        if triggered:
            trade.status = TradeStatus.OPEN
            trade.entry_triggered_at = datetime.now()
            trade.highest_price = ltp if trade.action == "BUY" else trade.highest_price
            trade.lowest_price = ltp if trade.action == "SELL" else trade.lowest_price
            logger.info(f"Trade #{trade.id} OPENED | {trade.action} {trade.symbol} @ {ltp}")
            self._notify(trade, f"ENTRY | {trade.action} {trade.symbol} @ {ltp:.2f}")

    def _check_exit(self, trade: Trade, ltp: float, db: Session) -> str | None:
        action = trade.action
        targets = [t for t in [trade.target1, trade.target2, trade.target3] if t]

        # 1. Check target hits and move SL accordingly
        current_target_idx = self._get_target_idx(trade)
        new_sl, new_target_idx, exit_reason = TrailingProfit.check_targets(
            action, ltp, trade.entry_price, targets, trade.trailing_sl or trade.stop_loss, current_target_idx
        )
        if new_sl != trade.trailing_sl:
            trade.trailing_sl = new_sl
        if exit_reason:
            self._close_trade(trade, ltp, exit_reason, db)
            return exit_reason

        # 2. Check Trailing SL
        if trade.trailing_sl_points and trade.trailing_sl:
            new_tsl, new_high, new_low, tsl_triggered = TrailingStopLoss.update(
                action, ltp,
                current_sl=trade.trailing_sl,
                entry_price=trade.entry_price,
                trailing_points=trade.trailing_sl_points,
                highest_price=trade.highest_price,
                lowest_price=trade.lowest_price,
            )
            trade.trailing_sl = new_tsl
            trade.highest_price = new_high
            trade.lowest_price = new_low

            if tsl_triggered:
                self._close_trade(trade, ltp, TradeStatus.TRAILING_SL_HIT, db)
                return TradeStatus.TRAILING_SL_HIT

        # 3. Check initial SL (skip if no SL set)
        if trade.stop_loss and TrailingProfit.check_sl(action, ltp, trade.stop_loss):
            self._close_trade(trade, ltp, TradeStatus.SL_HIT, db)
            return TradeStatus.SL_HIT

        return None

    def _get_target_idx(self, trade: Trade) -> int:
        """Determine which targets have already been passed based on trailing_sl vs stop_loss"""
        if trade.trailing_sl and trade.trailing_sl > trade.stop_loss and trade.action == "BUY":
            if trade.target2 and trade.trailing_sl >= trade.target1:
                return 2
            elif trade.target1 and trade.trailing_sl > trade.stop_loss:
                return 1
        return 0

    def _close_trade(self, trade: Trade, exit_price: float, reason: str, db: Session):
        trade.status = TradeStatus.CLOSED
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.closed_at = datetime.now()

        multiplier = 1 if trade.action == "BUY" else -1
        trade.gross_pnl = multiplier * (exit_price - trade.entry_price) * trade.quantity
        trade.net_pnl = trade.gross_pnl  # paper trade: no brokerage

        logger.info(
            f"Trade #{trade.id} CLOSED | {reason} | "
            f"Exit={exit_price:.2f} | PnL={trade.gross_pnl:.2f}"
        )
        self._notify(
            trade,
            f"EXIT [{reason}] | {trade.symbol} @ {exit_price:.2f} | P&L: ₹{trade.gross_pnl:.2f}"
        )

    def _notify(self, trade: Trade, message: str):
        try:
            from notifications.telegram_bot import telegram_bot
            telegram_bot.send(message)
        except Exception:
            pass  # Telegram optional

    def close_all_intraday(self):
        """Close all open intraday positions (called at session end)"""
        db = get_session()
        try:
            for trade_id, trade in list(self._active_trades.items()):
                trade = db.merge(trade)
                if trade.trade_type == "INTRADAY" and trade.status == TradeStatus.OPEN:
                    symbol = trade.symbol
                    token = self._symbol_token_map.get(symbol)
                    ltp = market_feed.get_ltp(token) if token else trade.entry_price
                    self._close_trade(trade, ltp or trade.entry_price, "SESSION_END", db)
            db.commit()
        finally:
            db.close()

    def get_open_trades(self) -> list[Trade]:
        db = get_session()
        try:
            return db.query(Trade).filter(
                Trade.status.in_([TradeStatus.OPEN, TradeStatus.PENDING])
            ).all()
        finally:
            db.close()


# Singleton
engine = PaperTradingEngine()
