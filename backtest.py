import argparse
import sys
import os
import math
import time
import numpy as np
import pandas as pd
import pytz
from loguru import logger

# Insert paths to discover modules smoothly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings, indicator_config as C
from data.fyers_client import fyers_client

IST = pytz.timezone("Asia/Kolkata")


class PortfolioBacktester:
    """Production Multi-Slot Portfolio Engine with Time-Stop Extension and Trail-Max Logic."""

    def __init__(self, capital: float = 500000.0, max_slots: int = 4):
        self.initial_capital = capital
        self.capital = capital
        self.max_slots = max_slots
        self.positions = {}  
        self.trades = []
        self.cooldowns = {}

    def compute_gatekeeper_score(self, snap) -> int:
        if getattr(snap, "setup_qualified", False):
            return 5
        return 0

    def run(self, universe_data: dict, lookback_days: int = 730):
        from indicators.engine import indicator_engine
        
        all_dates = set()
        for sym, df in universe_data.items():
            if "date" in df.columns:
                dates = pd.to_datetime(df["date"]).dt.date.tolist()
                all_dates.update(dates)
        trading_days = sorted(list(all_dates))

        warmup = C.MIN_HISTORY_DAYS
        if len(trading_days) <= warmup:
            logger.error("Data timeline pool depth too low.")
            return

        test_days = trading_days[warmup:]
        logger.info(f"Onlining re-engineered multi-slot walk-forward sequence across {len(test_days)} windows...")

        for today in test_days:
            day_data = {}
            for sym, df in universe_data.items():
                hist = df[pd.to_datetime(df["date"]).dt.date <= today].copy()
                if len(hist) >= warmup:
                    day_data[sym] = hist

            if not day_data:
                continue

            snapshots = {}
            for sym, df in day_data.items():
                snap = indicator_engine.compute(sym, df)
                if snap is not None:
                    snap.entry_score = self.compute_gatekeeper_score(snap)
                    snapshots[sym] = snap

            prices = {sym: float(df.iloc[-1]["close"]) for sym, df in day_data.items()}
            highs = {sym: float(df.iloc[-1]["high"]) for sym, df in day_data.items()}
            lows = {sym: float(df.iloc[-1]["low"]) for sym, df in day_data.items()}

            # 1. PROCESS PERFORMANCE-VALUED PORTFOLIO EXITS FIRST
            self._check_exits(today, prices, highs, lows, snapshots)

            # 2. PROCESS ENTRIES SECOND
            self._try_entries(today, snapshots, prices)

        active_symbols = list(self.positions.keys())
        for sym in active_symbols:
            final_price = prices.get(sym, self.positions[sym]["entry_price"])
            self._close_position(trading_days[-1], sym, final_price, "end_of_backtest")

        self._print_results()

    def _try_entries(self, date, snapshots, prices):
        available_slots = self.max_slots - len(self.positions)
        if available_slots <= 0:
            return

        valid_candidates = []
        for sym, snap in snapshots.items():
            if sym in self.positions:
                continue
            if sym in self.cooldowns and date <= self.cooldowns[sym]:
                continue
            if getattr(snap, "setup_qualified", False):
                valid_candidates.append((sym, snap))

        if not valid_candidates:
            return

        qualified_setups = []
        for sym, snap in valid_candidates:
            price = prices.get(sym, snap.close)
            atr = getattr(snap, "atr", 0.0)
            if atr <= 0:
                continue
                
            stop_loss = round(price - (C.ATR_STOP_MULT * atr), 2)
            target1 = round(price + (C.ATR_TARGET1_MULT * atr), 2)
            target2 = round(price + (C.ATR_TARGET2_MULT * atr), 2)
            
            stop_dist = max(0.01, price - stop_loss)
            qualified_setups.append((sym, snap, stop_loss, target1, target2, stop_dist, atr))

        qualified_setups.sort(key=lambda x: x[1].screener_score, reverse=True)

        for sym, snap, stop_loss, target1, target2, stop_dist, atr in qualified_setups:
            if len(self.positions) >= self.max_slots:
                break

            price = prices.get(sym, snap.close)
            total_equity = self._get_total_equity(prices)
            
            risk_amount = total_equity * (settings.risk_per_trade_pct / 100)
            qty = math.floor(risk_amount / stop_dist)
            
            position_cost = qty * price
            max_slot_cost = total_equity / float(self.max_slots)
            if position_cost > max_slot_cost:
                qty = math.floor(max_slot_cost / price)
                position_cost = qty * price

            if qty <= 0 or position_cost > self.capital:
                continue

            self.capital -= position_cost

            self.positions[sym] = {
                "symbol": sym,
                "entry_price": price,
                "position_cost_basis": position_cost,
                "quantity": qty,
                "qty_remaining": qty,
                "stop_loss": stop_loss,
                "trailing_stop": stop_loss,
                "target1": target1,
                "target2": target2,
                "entry_date": date,
                "target1_hit": False,
                "highest_seen_high": price,  # Tracks peak expansion boundaries
                "atr_at_entry": atr
            }
            logger.success(f"🚀 [PORTFOLIO ENTRY] {date} | Allocated Slot for {sym} @ ₹{price:.2f} | Units: {qty}")

    def _check_exits(self, date, prices, highs, lows, snapshots):
        active_symbols = list(self.positions.keys())
        
        for sym in active_symbols:
            pos = self.positions[sym]
            price = prices.get(sym, pos["entry_price"])
            current_high = highs.get(sym, price)
            current_low = lows.get(sym, price)
            snap = snapshots.get(sym)

            # Update maximum observed peak structure during the lifespan of the trade
            if current_high > pos["highest_seen_high"]:
                pos["highest_seen_high"] = current_high

            # INSTITUTIONAL MODIFICATION 2: Scale out ONLY 33% at Target 1, protect remaining 67%
            if not pos["target1_hit"] and price >= pos["target1"]:
                one_third_qty = math.ceil(pos["quantity"] * 0.33)
                if one_third_qty > 0 and pos["qty_remaining"] > one_third_qty:
                    pnl = (pos["target1"] - pos["entry_price"]) * one_third_qty
                    self.capital += (one_third_qty * pos["target1"])
                    pos["qty_remaining"] -= one_third_qty
                    pos["target1_hit"] = True
                    pos["trailing_stop"] = max(pos["trailing_stop"], pos["entry_price"])
                    
                    self.trades.append({
                        "date": date, "symbol": sym, "type": "partial_exit",
                        "entry": pos["entry_price"], "exit": pos["target1"],
                        "qty": one_third_qty, "pnl": pnl, "reason": "STRUCTURAL_TARGET1_HIT",
                    })
                    logger.info(f"🎯 [PARTIAL SHAVE] {date} | Scalped 33% of {sym} @ ₹{pos['target1']:.2f}. Core runner remains active.")

            # INSTITUTIONAL MODIFICATION 3: Activate Chandelier Trail relative to Peak Highs after Target 1
            if pos["target1_hit"]:
                dynamic_trail_floor = round(pos["highest_seen_high"] - (2.0 * pos["atr_at_entry"]), 2)
                pos["trailing_stop"] = max(pos["trailing_stop"], dynamic_trail_floor)

            effective_stop = max(pos["trailing_stop"], pos["stop_loss"])
            exit_reason = None
            exit_price = price

            # Evaluate Exit Vectors
            if price <= effective_stop:
                exit_reason = "HARD_STOP_LOSS_TRIPPED" if not pos["target1_hit"] else "DYNAMIC_TRAIL_STOP_HIT"
                exit_price = effective_stop
            elif price >= pos["target2"]:
                exit_reason = "STRUCTURAL_TARGET2_HIT"
                exit_price = pos["target2"]
            elif snap and getattr(snap, "rsi_overbought", False) and pos["target1_hit"]:
                exit_reason = "RSI_EXTREME_OVERBOUGHT"
                exit_price = price
            elif (date - pos["entry_date"]).days >= 21:
                # INSTITUTIONAL MODIFICATION 1: Time-Stop Extension Clause
                # If the asset is currently yielding positive returns, bypass time stops entirely
                if price > pos["entry_price"] * 1.02:
                    pass 
                else:
                    exit_reason = "TIME_STOP_EXPIRED"
                    exit_price = price

            if exit_reason:
                self._close_position(date, sym, exit_price, exit_reason)

    def _close_position(self, date, symbol, price, reason):
        pos = self.positions.get(symbol)
        if not pos:
            return
            
        qty = pos["qty_remaining"]
        pnl = (price - pos["entry_price"]) * qty
        self.capital += (qty * price)
        
        self.trades.append({
            "date": date, "symbol": symbol, "type": "full_exit",
            "entry": pos["entry_price"], "exit": price, "qty": qty, "pnl": pnl, "reason": reason,
            "hold_days": (date - pos["entry_date"]).days if hasattr(date - pos["entry_date"], "days") else 0,
        })
        
        logger.warning(f"🚪 [SLOT FREE] {date} | Full Liquidated {symbol} @ ₹{price:.2f} | PnL: ₹{pnl:+.2f} | Reason: {reason}")
        self.cooldowns[symbol] = date + pd.Timedelta(days=5)
        del self.positions[symbol]

    def _get_total_equity(self, current_prices: dict) -> float:
        floating_value = 0.0
        for sym, pos in self.positions.items():
            price = current_prices.get(sym, pos["entry_price"])
            floating_value += pos["qty_remaining"] * price
        return self.capital + floating_value

    def _print_results(self):
        full_exits = [t for t in self.trades if t["type"] == "full_exit"]
        true_net_pnl = self.capital - self.initial_capital
        pnl_percentage = (true_net_pnl / self.initial_capital) * 100
        
        all_pnl_records = [t["pnl"] for t in self.trades]
        wins = [p for p in all_pnl_records if p > 0]
        losses = [p for p in all_pnl_records if p <= 0]

        win_rate = (len(wins) / (len(wins) + len(losses)) * 100) if (len(wins) + len(losses)) > 0 else 0.0
        avg_win = np.mean(wins) if wins else 0.0
        avg_loss = np.mean(losses) if losses else 0.0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses else 0.0

        print("\n" + "=" * 60)
        print("          PRODUCTION PORTFOLIO ENGINE: AUDITED RESULTS")
        print("=" * 60)
        print(f"Starting Fund Equity: ₹ {self.initial_capital:14,.2f}")
        print(f"Ending Fund Equity:   ₹ {self.capital:14,.2f}")
        print(f"Audited Net P&L:      ₹ {true_net_pnl:14,.2f} ({pnl_percentage:+.2f}%)")
        print(f"Total Closed Cycles:  {len(full_exits)}")
        print(f"Portfolio Win Rate:   {win_rate:.1f}%")
        print(f"Average Win Run:      ₹ {avg_win:14,.2f}")
        print(f"Average Loss Stop:    ₹ {avg_loss:14,.2f}")
        print(f"System Profit Factor: {profit_factor:.2f}")
        print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest Multi-Slot Portfolio Engine")
    parser.add_argument("--days", type=int, default=730, help="Two year tracking data runway (730 days)")
    parser.add_argument("--capital", type=float, default=500000.0, help="Initial capital size in INR")
    args = parser.parse_args()

    logger.info("Connecting data transmission interface to Fyers servers...")
    if not fyers_client.connect():
        sys.exit(1)

    logger.info(f"Ingesting {args.days} trading days of clean Daily Close bars...")
    processed_universe = {}
    
    for sym in settings.nse_watchlist:
        try:
            df = fyers_client.get_historical(sym, days=args.days, resolution="D")
            if df is not None and not df.empty:
                df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
                processed_universe[sym] = df
            time.sleep(0.1)
        except Exception:
            continue

    if not processed_universe:
        print("❌ Ingestion error: Processed universe data returned empty.")
        sys.exit(1)

    bt = PortfolioBacktester(capital=args.capital, max_slots=4)
    bt.run(processed_universe, lookback_days=args.days)