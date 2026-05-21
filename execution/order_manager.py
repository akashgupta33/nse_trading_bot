import json
import math
from datetime import datetime
from typing import Optional
from loguru import logger
import pytz

from config.settings import settings
from utils.logger import trade_db  # Import our SQLite Database!

IST = pytz.timezone("Asia/Kolkata")


class Position:
    """Represents an open institutional swing trade with exact unit state tracking."""

    def __init__(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        target1: float,
        target2: float,
        target3: float,
        reason: str = "",
        conviction: float = 0.0,
        entry_time: Optional[datetime] = None,
    ):
        self.symbol = symbol
        self.entry_price = entry_price
        self.quantity = quantity
        self.qty_remaining = quantity
        
        self.stop_loss = stop_loss
        self.trailing_stop = stop_loss  # Hard baseline floor
        
        self.target1 = target1
        self.target2 = target2
        self.target3 = target3
        
        self.entry_time = entry_time or datetime.now(IST)
        self.reason = reason
        self.conviction = conviction
        
        # State Tracking for Claude's Sentinel Desks
        self.target1_hit = False
        self.target2_hit = False
        self.stagnation_reviewed = False

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "qty_remaining": self.qty_remaining,
            "stop_loss": self.stop_loss,
            "trailing_stop": self.trailing_stop,
            "target1": self.target1,
            "target2": self.target2,
            "target3": self.target3,
            "entry_time": self.entry_time.isoformat(),
            "reason": self.reason,
            "conviction": self.conviction,
            "target1_hit": self.target1_hit,
            "target2_hit": self.target2_hit,
            "stagnation_reviewed": self.stagnation_reviewed,
        }

    def unrealised_pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.qty_remaining

    def pnl_pct(self, current_price: float) -> float:
        return ((current_price - self.entry_price) / self.entry_price) * 100


class OrderManager:
    """Manages live order routing via Fyers socket layers or local simulation logs."""

    def __init__(self):
        self.positions = {}        # Active positions tracking dictionary: {symbol: Position}
        self.trade_log = []

    @property
    def has_active_positions(self) -> bool:
        return len(self.positions) > 0

    @property
    def open_positions_dict(self) -> dict:
        return {sym: pos.to_dict() for sym, pos in self.positions.items()}

    # =============================================================================
    # # Entry Routines
    # =============================================================================

    def enter_trade_from_watchlist(
        self, symbol: str, entry_price: float, quantity: int,
        stop_loss: float, target1: float, target2: float, target3: float,
        reason: str, conviction: float
    ) -> Optional[dict]:
        """Validates bounds and submits new orders routed from the Execution Agent."""
        
        if symbol in self.positions:
            logger.warning(f"Aborting execution: Position track already active for {symbol}")
            return None

        if len(self.positions) >= settings.max_positions:
            logger.warning(f"Portfolio matrix full ({len(self.positions)}/{settings.max_positions} slots). Aborting {symbol}.")
            return None

        if quantity <= 0:
            logger.warning(f"Execution Agent passed quantity of 0 for {symbol}. Aborting.")
            return None

        logger.info(f"🚀 [ENTRY INITIALIZED] {symbol} | Qty={quantity} @ Price=₹{entry_price}")

        # Routing to Live vs Paper Environment
        order_id = f"SIM-{datetime.now(IST).strftime('%Y%m%d%H%M%S')}"
        actual_price = entry_price

        if settings.is_live:
            order_result = self._place_live_order(symbol, quantity, "BUY")
            if not order_result:
                logger.error(f"Fyers execution rejection generated on broker bridge for {symbol}")
                return None
            order_id = order_result.get("order_id", order_id)
            actual_price = order_result.get("avg_price", entry_price) if order_result.get("avg_price", 0) > 0 else entry_price

        # Open Position object tracking reference
        pos = Position(
            symbol=symbol,
            entry_price=actual_price,
            quantity=quantity,
            stop_loss=stop_loss,
            target1=target1,
            target2=target2,
            target3=target3,
            reason=reason,
            conviction=conviction
        )
        self.positions[symbol] = pos

        trade_record = {
            "type": "ENTRY",
            "symbol": symbol,
            "entry_price": actual_price,
            "qty": quantity,
            "stop_loss": stop_loss,
            "target1": target1,
            "target2": target2,
            "target3": target3,
            "reason": reason,
            "timestamp": datetime.now(IST).isoformat(),
            "order_id": order_id,
            "mode": "live" if settings.is_live else "paper",
        }

        self.trade_log.append(trade_record)
        trade_db.log_trade(trade_record)  # Persist to SQLite
        
        logger.success(f"✅ [ENTRY RECORDED] {symbol} allocation deployed successfully.")
        return trade_record

    # =============================================================================
    # # Exit Routines
    # =============================================================================

    def exit_trade_portfolio(self, symbol: str, current_price: float, reason: str = "signal_exit") -> Optional[dict]:
        """Liquidates remaining asset units (100% exit), adjusting the balance pool cleanly."""
        pos = self.positions.get(symbol)
        if not pos:
            logger.warning(f"Request dropped: No active tracking object matching {symbol}")
            return None

        qty_to_liquidate = pos.qty_remaining
        logger.info(f"🚪 [EXIT INITIALIZED] Closing {symbol} | Units remaining: {qty_to_liquidate} | Reason: {reason}")

        actual_price = current_price
        order_id = f"SIM-EX-{datetime.now(IST).strftime('%Y%m%d%H%M%S')}"

        if settings.is_live:
            order_result = self._place_live_order(symbol, qty_to_liquidate, "SELL")
            if not order_result:
                logger.error(f"Broker routing system dropped liquidation request for active holding {symbol}")
                return None
            order_id = order_result.get("order_id", order_id)
            actual_price = order_result.get("avg_price", current_price) if order_result.get("avg_price", 0) > 0 else current_price

        pnl = (actual_price - pos.entry_price) * qty_to_liquidate
        pnl_pct = ((actual_price - pos.entry_price) / pos.entry_price) * 100

        trade_record = {
            "type": "EXIT",
            "symbol": symbol,
            "entry_price": pos.entry_price,
            "exit_price": actual_price,
            "qty": qty_to_liquidate,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "hold_time": str(datetime.now(IST) - pos.entry_time),
            "timestamp": datetime.now(IST).isoformat(),
            "order_id": order_id,
            "mode": "live" if settings.is_live else "paper",
        }

        self.trade_log.append(trade_record)
        trade_db.log_trade(trade_record)  # Persist to SQLite
        del self.positions[symbol]
        
        logger.info(f"🚪 [PORTFOLIO SLOT CLEARED] Closed {symbol} Net PnL: ₹{pnl:.2f} ({pnl_pct:.2f}%)")
        return trade_record

    def partial_exit(self, symbol: str, current_price: float, qty_to_sell: int, reason: str = "target_hit") -> Optional[dict]:
        """Liquidates an explicit percentage allocation without dropping the structural tracking layer."""
        pos = self.positions.get(symbol)
        if not pos:
            return None

        qty = min(qty_to_sell, pos.qty_remaining)
        if qty <= 0:
            return None

        logger.info(f"💵 [PARTIAL DEPLOYMENT EXECUTED] Shaving {qty} shares from {symbol}. Reason: {reason}")

        actual_price = current_price
        order_id = f"SIM-PE-{datetime.now(IST).strftime('%Y%m%d%H%M%S')}"

        if settings.is_live:
            order_result = self._place_live_order(symbol, qty, "SELL")
            actual_price = order_result.get("avg_price", current_price) if order_result else current_price
            if order_result:
                order_id = order_result.get("order_id", order_id)

        pnl = (actual_price - pos.entry_price) * qty
        
        # Deduct volume cleanly from our tracking states
        pos.qty_remaining -= qty

        record = {
            "type": "PARTIAL_EXIT",
            "symbol": symbol,
            "exit_price": actual_price,
            "qty": qty,
            "pnl": round(pnl, 2),
            "reason": reason,
            "order_id": order_id,
            "timestamp": datetime.now(IST).isoformat(),
        }

        self.trade_log.append(record)
        trade_db.log_trade(record)  # Persist to SQLite
        logger.info(f"⚖️ [PORTFOLIO BALANCE UPDATED] Partials cleared for {symbol}. Adjusted floor: ₹{pos.trailing_stop}")
        
        # If we accidentally sold everything due to rounding, clear the slot fully
        if pos.qty_remaining <= 0:
            del self.positions[symbol]
            
        return record

    # =============================================================================
    # # Broker Integration Interface Bridge
    # =============================================================================

    def _place_live_order(self, symbol: str, qty: int, side: str) -> Optional[dict]:
        """Submits structural order dictionary vectors straight to live Fyers API endpoints."""
        try:
            from data.fyers_client import fyers_client
            
            # CNC (Cash 'N' Carry) configuration handles institutional overnight holding streams
            order_data = {
                "symbol": symbol,
                "qty": qty,
                "type": 3,              # 3 = Direct Market Execution routing
                "side": 1 if side == "BUY" else -1,
                "productType": "CNC",   # INSTITUTIONAL SWING DELIVERY
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": "False",
            }

            response = fyers_client.fyers.place_order(data=order_data)
            if response and response.get("s") == "ok":
                return {"order_id": response.get("id"), "avg_price": 0, "status": "placed"}
            else:
                logger.error(f"Fyers exchange rejected submission routing array: {response}")
                return None
        except Exception as e:
            logger.error(f"Critical connection bridge interruption failing order dispatch: {e}")
            return None

    # =============================================================================
    # # System Ingestion Getters
    # =============================================================================

    def get_trade_log(self) -> list:
        return self.trade_log.copy()

    def get_daily_pnl(self) -> float:
        return round(sum(t.get("pnl", 0.0) for t in self.trade_log if t.get("type") in ["EXIT", "PARTIAL_EXIT"]), 2)


# Unified Singleton Instance Reference Mapping
order_manager = OrderManager()