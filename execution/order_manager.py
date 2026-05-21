import json
import math
from datetime import datetime
from typing import Optional
from loguru import logger
import pytz

from config.settings import settings, indicator_config as C

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
        atr: float,
        entry_time: Optional[datetime] = None,
        reason: str = "",
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
        
        self.atr = atr
        self.entry_time = entry_time or datetime.now(IST)
        self.reason = reason
        self.target1_hit = False

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
            "atr": self.atr,
            "entry_time": self.entry_time.isoformat(),
            "reason": self.reason,
            "target1_hit": self.target1_hit,
        }

    def unrealised_pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.qty_remaining

    def pnl_pct(self, current_price: float) -> float:
        return ((current_price - self.entry_price) / self.entry_price) * 100


class RiskGuard:
    """Enforces Turtle Trader Risk Parity constraints across active cash balances."""

    def check_new_entry(self, snapshot, capital: float, current_positions_count: int) -> tuple[bool, str]:
        """Returns (allowed, reason) after verifying structural verification boundaries."""
        # 1. Verify the setup has qualified past our 5 volume/direction gates
        if not getattr(snapshot, "setup_qualified", False):
            return False, "Technical confirmation gates rejected setup (RSI or volume confirmation missing)"

        # 2. Portfolio Slot Boundary Constraint Check
        if current_positions_count >= settings.max_positions:
            return False, f"Portfolio matrix full ({current_positions_count}/{settings.max_positions} slots occupied)"

        # 3. Prevent zero division tracking errors
        if snapshot.atr <= 0:
            return False, "ATR value is zero - cannot build structural volatility stop loss floor"

        # 4. Quantity Validation Math
        stop_dist = snapshot.close - snapshot.stop_loss_price
        if stop_dist <= 0:
            return False, f"Invalid stop loss boundary structure distance: {stop_dist:.2f}"

        # 5. Asset Position Allocation Check
        cash_at_risk = capital * (settings.risk_per_trade_pct / 100)
        qty = math.floor(cash_at_risk / stop_dist)
        if qty <= 0:
            return False, f"Calculated position unit allocation evaluates to 0. Risk envelope too compressed."

        return True, "All quantitative risk parameters cleared."

    def calculate_quantity(self, close_price: float, stop_loss_price: float, total_equity: float) -> int:
        """Calculates strict Risk Parity share volume bound by a hard 1/4 slot cost ceiling."""
        stop_dist = close_price - stop_loss_price
        if stop_dist <= 0:
            return 0

        # Calculate base unit parity size (1.5% max account risk allocation)
        cash_at_risk = total_equity * (settings.risk_per_trade_pct / 100)
        qty = math.floor(cash_at_risk / stop_dist)
        position_cost = qty * close_price

        # Enforce maximum single slot cost ceiling (Total Equity / Max Positions)
        max_slot_cost = total_equity / settings.max_positions
        if position_cost > max_slot_cost:
            qty = math.floor(max_slot_cost / close_price)

        return max(0, qty)


class OrderManager:
    """Manages live order routing via Fyers socket layers or local simulation logs."""

    def __init__(self):
        self.positions = {}        # Active positions tracking dictionary: {symbol: Position}
        self.trade_log = []
        self.risk = RiskGuard()

    @property
    def has_active_positions(self) -> bool:
        return len(self.positions) > 0

    @property
    def open_positions_dict(self) -> dict:
        return {sym: pos.to_dict() for sym, pos in self.positions.items()}

    # =============================================================================
    # # Entry Routines
    # =============================================================================

    def enter_trade(self, snapshot, reason: str = "") -> Optional[dict]:
        """Validates indicators and submits new orders to the delivery engine pipeline."""
        if snapshot.symbol in self.positions:
            logger.warning(f"Aborting execution: Position track already active for {snapshot.symbol}")
            return None

        # Fetch active capital metrics base
        capital = self._get_active_capital_base()
        
        allowed, msg = self.risk.check_new_entry(snapshot, capital, len(self.positions))
        if not allowed:
            logger.warning(f"RiskGuard BLOCKED execution for {snapshot.symbol}: {msg}")
            return None

        qty = self.risk.calculate_quantity(snapshot.close, snapshot.stop_loss_price, capital)
        if qty <= 0:
            logger.warning(f"Risk module calculated empty trade size for {snapshot.symbol}. Aborting.")
            return None

        price = snapshot.close
        logger.info(f"🚀 [ENTRY CRON INITIALIZED] {snapshot.symbol} | Qty={qty} @ Price=₹{price}")

        # Routing to Live vs Paper Environment
        order_id = f"SIM-{datetime.now(IST).strftime('%Y%m%d%H%M%S')}"
        actual_price = price

        if settings.is_live:
            order_result = self._place_live_order(snapshot.symbol, qty, "BUY")
            if not order_result:
                logger.error(f"Fyers execution rejection generated on broker bridge for {snapshot.symbol}")
                return None
            order_id = order_result.get("order_id", order_id)
            actual_price = order_result.get("avg_price", price) if order_result.get("avg_price", 0) > 0 else price

        # Open Position object tracking reference
        pos = Position(
            symbol=snapshot.symbol,
            entry_price=actual_price,
            quantity=qty,
            stop_loss=snapshot.stop_loss_price,
            target1=snapshot.target1_price,
            target2=snapshot.target2_price,
            target3=snapshot.target3_price,
            atr=snapshot.atr,
            reason=reason,
        )
        self.positions[snapshot.symbol] = pos

        trade_record = {
            "type": "ENTRY",
            "symbol": snapshot.symbol,
            "price": actual_price,
            "qty": qty,
            "stop_loss": snapshot.stop_loss_price,
            "target1": snapshot.target1_price,
            "target2": snapshot.target2_price,
            "target3": snapshot.target3_price,
            "atr": snapshot.atr,
            "reason": reason,
            "timestamp": datetime.now(IST).isoformat(),
            "order_id": order_id,
            "mode": "live" if settings.is_live else "paper",
        }

        self.trade_log.append(trade_record)
        logger.success(f"✅ [ENTRY RECORDED] {snapshot.symbol} allocation deployed successfully.")
        return trade_record

    # =============================================================================
    # # Exit Routines
    # =============================================================================

    def exit_trade_portfolio(self, symbol: str, current_price: float, reason: str = "signal_exit") -> Optional[dict]:
        """Liquidates remaining asset units, adjusting the balance pool cleanly."""
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

        # Accurate PnL calculations linked explicitly to final remaining volumes
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
        del self.positions[symbol]
        
        logger.info(f"🚪 [PORTFOLIO SLOT CLEARED] Closed {symbol} Net PnL: ₹{pnl:.2f} ({pnl_pct:.2f}%)")
        return trade_record

    def partial_exit(self, symbol: str, current_price: float, qty_to_sell: int, reason: str = "target1_hit") -> Optional[dict]:
        """Liquidates an explicit percentage allocation without dropping the structural tracking layer."""
        pos = self.positions.get(symbol)
        if not pos:
            return None

        # Absolute constraint safety bounds check
        qty = min(qty_to_sell, pos.qty_remaining)
        if qty <= 0:
            return None

        logger.info(f"💵 [PARTIAL DEPLOYMENT EXECUTED] Shaving {qty} shares from {symbol} position array. Target: {reason}")

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
        pos.target1_hit = True
        
        # Pull structural stop floor to breakeven parameter bounds
        pos.trailing_stop = max(pos.trailing_stop, pos.entry_price)

        record = {
            "type": "PARTIAL_EXIT",
            "symbol": symbol,
            "exit_price": actual_price,
            "qty": qty,
            "pnl": round(pnl, 2),
            "reason": reason,
            "remaining_qty": pos.qty_remaining,
            "new_stop": pos.trailing_stop,
            "order_id": order_id,
            "timestamp": datetime.now(IST).isoformat(),
        }

        self.trade_log.append(record)
        logger.info(f"⚖️ [PORTFOLIO BALANCE UPDATED] Partials cleared for {symbol}. Adjusted floor: ₹{pos.trailing_stop}")
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
                "productType": "CNC",   # INSTITUTIONAL SWING DELIVERY SETTING (Changed from INTRADAY)
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": "False",
            }

            response = fyers_client.self_fyers.place_order(data=order_data)
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

    def _get_active_capital_base(self) -> float:
        """Returns liquid cash plus the market valuation of open asset arrays."""
        if not settings.is_live:
            return settings.capital
            
        try:
            from data.fyers_client import fyers_client
            live_cash = fyers_client.get_funds()
            if live_cash is not None:
                return float(live_cash)
        except Exception as e:
            logger.error(f"Error reading live cash balances: {e}")
        return settings.capital

    def get_trade_log(self) -> list:
        return self.trade_log.copy()

    def get_daily_pnl(self) -> float:
        return round(sum(t.get("pnl", 0.0) for t in self.trade_log if t.get("type") in ["EXIT", "PARTIAL_EXIT"]), 2)

    def has_position_symbol(self, symbol: str) -> bool:
        return symbol in self.positions


# Unified Singleton Instance Reference Mapping
order_manager = OrderManager()