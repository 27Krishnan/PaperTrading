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
import json
from datetime import datetime
from typing import Optional
from collections import defaultdict
from sqlalchemy.orm import Session
from database.models import Trade, TradeStatus
from database.db import get_session
from data.market_feed import market_feed
from data.angel_api import angel_api
from strategies.trailing_sl import TrailingStopLoss
from strategies.trailing_profit import TrailingProfit
from scheduler.market_sessions import get_exchange_for_symbol
from core.utils import get_now_ist
from loguru import logger


class PaperTradingEngine:

    def __init__(self):
        self._active_trades: dict[int, Trade] = {}  # trade_id → Trade
        self._symbol_token_map: dict[str, str] = {}  # symbol → token
        self._token_to_trades: dict[str, list[int]] = defaultdict(list) # token → [trade_id]
        market_feed.add_callback(self._on_tick)
        # _load_active_trades() should be called from lifespan after connecting to Angel One

    def _load_active_trades(self):
        """Reload OPEN and PENDING trades from DB on engine startup"""
        db = get_session()
        try:
            active = db.query(Trade).filter(
                Trade.status.in_([TradeStatus.OPEN, TradeStatus.PENDING])
            ).all()
            for trade in active:
                self._active_trades[trade.id] = trade
                
                # RE-POPULATE mappings so _on_tick finds these trades
                symbol = trade.symbol
                token = self._symbol_token_map.get(symbol)
                if not token:
                    token = angel_api.get_token(trade.exchange, symbol)
                    if token:
                        self._symbol_token_map[symbol] = token
                
                if token:
                    if trade.id not in self._token_to_trades[token]:
                        self._token_to_trades[token].append(trade.id)
                    market_feed.subscribe(token, symbol, trade.exchange)

            if active:
                logger.info(f"Engine recovered {len(active)} active trades from database")
                if not market_feed._running:
                    market_feed.start()
        except Exception as e:
            logger.error(f"Error recovering active trades: {e}")
        finally:
            db.close()

    def _add_audit_log(self, trade: Trade, message: str, type: str = "INFO", ltp: float = None):
        """Append a timestamped event to the trade's audit log"""
        log_entry = {
            "time": get_now_ist().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "type": type,
            "msg": message,
            "ltp": ltp
        }
        
        try:
            logs = json.loads(trade.audit_log) if trade.audit_log else []
        except:
            logs = []
            
        logs.append(log_entry)
        trade.audit_log = json.dumps(logs)

    def add_trade(self, signal: dict, lot_size: int = 1,
                  trailing_sl_points: float = None,
                  trailing_method: str = "sl_distance",
                  owner_id: int = None,
                  strategy: str = None) -> Trade | None:
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
                target_idx=0,
                owner_id=owner_id,
                strategy=strategy,
            )
            self._add_audit_log(trade, f"Signal Received: {action} {trade.symbol} @ {entry or 'Market'}. Type: {trade.entry_type}. SL: {sl}. T1: {targets[0] if targets else 'None'}", type="RECEIVED")
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
                self._token_to_trades[token].append(trade.id)
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
        trade_ids = self._token_to_trades.get(token, [])
        for trade_id in list(trade_ids):
            self.process_ltp(trade_id, ltp)

    def _check_entry(self, trade: Trade, ltp: float, db: Session):
        triggered = TrailingProfit.check_entry_trigger(
            trade.action, ltp, trade.entry_price, trade.entry_type
        )
        
        if not triggered and trade.status == TradeStatus.PENDING:
            # Periodically log "Waiting" if ltp moves significantly or first check
            pass # Skipping verbose tick logs to keep audit clean

        if triggered:
            trade.status = TradeStatus.OPEN
            trade.entry_triggered_at = get_now_ist()
            trade.highest_price = ltp if trade.action == "BUY" else trade.highest_price
            trade.lowest_price = ltp if trade.action == "SELL" else trade.lowest_price
            msg = f"Order Executed @ {ltp:.2f}. Condition met for {trade.entry_type} {trade.entry_price}"
            self._add_audit_log(trade, msg, type="EXECUTED", ltp=ltp)
            logger.info(f"Trade #{trade.id} OPENED | {trade.action} {trade.symbol} @ {ltp}")
            self._notify(trade, f"ENTRY | {trade.action} {trade.symbol} @ {ltp:.2f}")

    def _check_exit(self, trade: Trade, ltp: float, db: Session) -> str | None:
        action = trade.action
        targets = [t for t in [trade.target1, trade.target2, trade.target3] if t]

        # 1. Check target hits and move SL accordingly
        current_target_idx = trade.target_idx or 0
        new_sl, new_target_idx, exit_reason = TrailingProfit.check_targets(
            action, ltp, trade.entry_price, targets, trade.trailing_sl or trade.stop_loss, current_target_idx
        )
        if new_sl != trade.trailing_sl:
            old_sl = trade.trailing_sl or trade.stop_loss
            trade.trailing_sl = new_sl
            self._add_audit_log(trade, f"SL Moved to {new_sl:.2f} due to Target Hit", type="LOGIC", ltp=ltp)
        if new_target_idx != current_target_idx:
            trade.target_idx = new_target_idx
            self._add_audit_log(trade, f"Target {new_target_idx} Hit @ {ltp:.2f}", type="TARGET", ltp=ltp)
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
            if new_tsl != trade.trailing_sl:
                self._add_audit_log(trade, f"Trailing SL Ratchet: {trade.trailing_sl:.2f} -> {new_tsl:.2f}", type="TRAIL", ltp=ltp)
            trade.trailing_sl = new_tsl
            trade.highest_price = new_high
            trade.lowest_price = new_low

            if tsl_triggered:
                self._close_trade(trade, ltp, TradeStatus.TRAILING_SL_HIT, db)
                return TradeStatus.TRAILING_SL_HIT

        # 3. Check protective SL (use the latest moved SL)
        active_sl = trade.trailing_sl if trade.trailing_sl is not None else trade.stop_loss
        if active_sl and TrailingProfit.check_sl(action, ltp, active_sl):
            self._close_trade(trade, ltp, TradeStatus.SL_HIT, db)
            return TradeStatus.SL_HIT

        return None

    def _close_trade(self, trade: Trade, exit_price: float, reason: str, db: Session):
        trade.status = TradeStatus.CLOSED
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.closed_at = get_now_ist()
        self._add_audit_log(trade, f"Trade Closed: {reason}", type="CLOSED", ltp=exit_price)

        multiplier = 1 if trade.action == "BUY" else -1
        # Fallback to entry_price if exit_price is missing
        exit_val = exit_price if exit_price is not None else trade.entry_price
        trade.exit_price = exit_val
        
        points = multiplier * (exit_val - trade.entry_price)
        trade.gross_pnl = float(points * trade.quantity)
        trade.net_pnl = trade.gross_pnl

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

    def close_all_intraday(self, exchange: str = None):
        """Close all open intraday positions (called at session end)"""
        db = get_session()
        try:
            for trade_id, trade in list(self._active_trades.items()):
                trade = db.merge(trade)
                
                # Filter by Intraday first
                if trade.trade_type != "INTRADAY":
                    continue
                    
                # Filter by Status
                if trade.status != TradeStatus.OPEN:
                    continue
                    
                # Optional Exchange Filter
                if exchange and trade.exchange.upper() != exchange.upper():
                    continue

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
