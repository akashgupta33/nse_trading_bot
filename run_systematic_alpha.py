import os
import math
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from loguru import logger

# =============================================================================
# 1. INSTITUTIONAL CONFIGURATION MATRIX
# =============================================================================
UNIVERSE = [
    "NSE:RELIANCE-EQ","NSE:TCS-EQ","NSE:HDFCBANK-EQ","NSE:ICICIBANK-EQ","NSE:INFY-EQ",
    "NSE:ITC-EQ","NSE:SBIN-EQ","NSE:BHARTIARTL-EQ","NSE:BAJFINANCE-EQ","NSE:LT-EQ",
    "NSE:MARUTI-EQ","NSE:SUNPHARMA-EQ","NSE:WIPRO-EQ","NSE:ULTRACEMCO-EQ","NSE:HCLTECH-EQ",
    "NSE:POWERGRID-EQ","NSE:NTPC-EQ","NSE:ONGC-EQ","NSE:COALINDIA-EQ","NSE:M&M-EQ",
    "NSE:TATAMOTORS-EQ","NSE:TATACONSUM-EQ","NSE:BAJAJFINSV-EQ","NSE:BEL-EQ","NSE:HINDALCO-EQ",
    "NSE:JSWSTEEL-EQ","NSE:TATASTEEL-EQ","NSE:HAVELLS-EQ","NSE:PIDILITIND-EQ","NSE:INDIGO-EQ",
    "NSE:ZOMATO-EQ","NSE:HAL-EQ","NSE:BHEL-EQ","NSE:IRCTC-EQ","NSE:COFORGE-EQ",
    "NSE:ASTRAL-EQ","NSE:SUPREMEIND-EQ","NSE:CONCOR-EQ","NSE:PETRONET-EQ","NSE:SAIL-EQ",
    "NSE:NMDC-EQ","NSE:VEDL-EQ","NSE:LUPIN-EQ","NSE:DEEPAKNTR-EQ","NSE:PIIND-EQ",
    "NSE:VOLTAS-EQ","NSE:APOLLOTYRE-EQ","NSE:EXIDEIND-EQ","NSE:DLF-EQ","NSE:PRESTIGE-EQ"
]

RISK_PER_TRADE_PCT = 1.5   # Maximum risk parameters
MAX_ACTIVE_SLOTS = 4       
CASH_RESERVE_PCT = 10.0    

SMA_TREND_LOOKBACK = 200   
RSI_PERIOD = 14
RSI_MIN_PULLBACK = 40.0    
RSI_MAX_PULLBACK = 57.0    # Optimized pullback tracking zone
ATR_PERIOD = 14
ATR_STOP_MULT = 2.5        
ATR_TARGET_MULT = 3.0      # Optimized 2:1 R:R profit realization target

# =============================================================================
# 2. CONFIRMED TREND RESUMPTION ENGINE
# =============================================================================
class CleanIndicatorEngine:
    """Computes mathematical indicator arrays with strict entry confirmation gates."""
    
    def compute_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy().sort_values("date").reset_index(drop=True)
        if len(df) < SMA_TREND_LOOKBACK:
            return pd.DataFrame()
            
        # 200 Day Institutional Trend Line
        df["sma200"] = df["close"].rolling(window=SMA_TREND_LOOKBACK).mean()
        df["macro_bullish"] = df["close"] > df["sma200"]
        
        # Relative Strength Index (RSI) Vector
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
        rs = gain / (loss + 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))
        
        # Basic Pullback Zone Boundaries
        df["rsi_in_zone"] = (df["rsi"] >= RSI_MIN_PULLBACK) & (df["rsi"] <= RSI_MAX_PULLBACK)
        
        # --- THE GOLDEN INSTITUTIONAL REVERSAL CONFIRMATION GATES ---
        # Gate A: RSI Momentum must actively be hook-turning upwards (Not in free-fall)
        df["rsi_turning_up"] = df["rsi"] > df["rsi"].shift(1)
        
        # Gate B: Price must close above the previous day's high (Price Action Breakout)
        df["price_breaking_up"] = df["close"] > df["high"].shift(1)
        
        # Gate C: Volume must back the reversal (At least 1.1x of the 20-day Volume MA)
        df["vol_ma20"] = df["volume"].rolling(window=20).mean()
        df["volume_confirmed"] = df["volume"] >= (df["vol_ma20"] * 1.1)
        
        # Average True Range (ATR) Volatility Tracker
        high_low = df["high"] - df["low"]
        high_cp = (df["high"] - df["close"].shift()).abs()
        low_cp = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
        df["atr"] = tr.rolling(window=ATR_PERIOD).mean()
        
        # Full Composite Strategy Condition Gate (All 5 must be True)
        df["signal_approved"] = (
            df["macro_bullish"] & 
            df["rsi_in_zone"] & 
            df["rsi_turning_up"] & 
            df["price_breaking_up"] &
            df["volume_confirmed"]
        )
        return df

# =============================================================================
# 3. AUDITED PORTFOLIO RISK PARITY MANAGER
# =============================================================================
class AuditedInstitutionalBacktester:
    def __init__(self, initial_capital: float = 500000.0):
        self.initial_capital = initial_capital
        self.current_cash = initial_capital
        self.open_positions = {}
        self.completed_ledger = []
        self.cooldown_tracker = {}

    def execute_simulation(self, universe_data: dict):
        all_dates = sorted(list(set(
            date for df in universe_data.values() for date in df["date"]
        )))
        
        indicator_engine = CleanIndicatorEngine()
        processed_universe = {}
        
        for sym, df in universe_data.items():
            signaled_df = indicator_engine.compute_signals(df)
            if not signaled_df.empty:
                processed_universe[sym] = signaled_df

        for today in all_dates:
            # 1. PROCESS EXITS
            active_symbols = list(self.open_positions.keys())
            for sym in active_symbols:
                pos = self.open_positions[sym]
                df = processed_universe[sym]
                today_row = df[df["date"] == today]
                if today_row.empty:
                    continue
                    
                row_data = today_row.iloc[0]
                current_close = float(row_data["close"])
                
                exit_reason = None
                exit_price = current_close
                
                if current_close <= pos["stop_loss"]:
                    exit_reason = "HARD_STOP_LOSS_TRIPPED"
                    exit_price = pos["stop_loss"]
                elif current_close >= pos["target"]:
                    exit_reason = "STRUCTURAL_TARGET_HIT"
                    exit_price = pos["target"]
                elif (today - pos["entry_date"]).days >= 45:
                    exit_reason = "TIME_STOP_EXPIRED"
                    exit_price = current_close
                    
                if exit_reason:
                    trade_pnl = (exit_price - pos["entry_price"]) * pos["qty"]
                    self.current_cash += (pos["qty"] * exit_price)
                    
                    self.completed_ledger.append({
                        "symbol": sym,
                        "entry_date": pos["entry_date"],
                        "exit_date": today,
                        "entry_price": pos["entry_price"],
                        "exit_price": exit_price,
                        "qty": pos["qty"],
                        "pnl": trade_pnl,
                        "reason": exit_reason
                    })
                    self.cooldown_tracker[sym] = today + timedelta(days=5)
                    del self.open_positions[sym]

            # 2. PROCESS ENTRIES
            available_slots = MAX_ACTIVE_SLOTS - len(self.open_positions)
            if available_slots <= 0:
                continue
                
            for sym, df in processed_universe.items():
                if len(self.open_positions) >= MAX_ACTIVE_SLOTS:
                    break
                if sym in self.open_positions:
                    continue
                if sym in self.cooldown_tracker and today <= self.cooldown_tracker[sym]:
                    continue
                    
                today_row = df[df["date"] == today]
                if today_row.empty:
                    continue
                    
                row_data = today_row.iloc[0]
                if not row_data["signal_approved"]:
                    continue
                    
                close_price = float(row_data["close"])
                atr_val = float(row_data["atr"])
                if pd.isna(atr_val) or atr_val <= 0:
                    continue
                    
                stop_loss = round(close_price - (ATR_STOP_MULT * atr_val), 2)
                target = round(close_price + (ATR_TARGET_MULT * atr_val), 2)
                stop_distance = close_price - stop_loss
                
                total_portfolio_equity = self.get_total_equity()
                fund_cash_at_risk = total_portfolio_equity * (RISK_PER_TRADE_PCT / 100)
                
                qty = math.floor(fund_cash_at_risk / stop_distance)
                position_cost = qty * close_price
                
                max_allowed_cost = total_portfolio_equity / MAX_ACTIVE_SLOTS
                if position_cost > max_allowed_cost:
                    qty = math.floor(max_allowed_cost / close_price)
                    position_cost = qty * close_price
                    
                if qty <= 0 or position_cost > self.current_cash:
                    continue
                    
                self.current_cash -= position_cost
                self.open_positions[sym] = {
                    "entry_date": today,
                    "entry_price": close_price,
                    "qty": qty,
                    "stop_loss": stop_loss,
                    "target": target
                }

        self._compile_final_audit_report(processed_universe)

    def get_total_equity(self) -> float:
        open_value = sum(pos["qty"] * pos["entry_price"] for pos in self.open_positions.values())
        return self.current_cash + open_value

    def _compile_final_audit_report(self, processed_universe):
        final_open_value = 0
        for sym, pos in self.open_positions.items():
            last_df = processed_universe[sym]
            final_open_value += pos["qty"] * float(last_df["close"].iloc[-1])
            
        ending_fund_equity = self.current_cash + final_open_value
        audited_net_pnl = ending_fund_equity - self.initial_capital
        pnl_pct = (audited_net_pnl / self.initial_capital) * 100
        
        total_trades = len(self.completed_ledger)
        winners = [t for t in self.completed_ledger if t["pnl"] > 0]
        losers = [t for t in self.completed_ledger if t["pnl"] <= 0]
        
        win_rate = (len(winners) / total_trades * 100) if total_trades > 0 else 0.0
        avg_win = np.mean([t["pnl"] for t in winners]) if winners else 0.0
        avg_loss = np.mean([t["pnl"] for t in losers]) if losers else 0.0
        
        gross_profit = sum([t["pnl"] for t in winners])
        gross_loss = abs(sum([t["pnl"] for t in losers]))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0.0

        print("\n" + "="*60)
        print("        AUDITED SYSTEM ACCOUNTING REPORT (VOLUME CONFIRMED)")
        print("="*60)
        print(f"Starting Fund Equity: ₹ {self.initial_capital:14,.2f}")
        print(f"Ending Fund Equity:   ₹ {ending_fund_equity:14,.2f}")
        print(f"Audited Net P&L:      ₹ {audited_net_pnl:14,.2f} ({pnl_pct:+.2f}%)")
        print(f"Total Closed Cycles:  {total_trades}")
        print(f"System Win Rate:      {win_rate:.1f}%")
        print(f"Average Winning Run:  ₹ {avg_win:14,.2f}")
        print(f"Average Losing Stop:  ₹ {avg_loss:14,.2f}")
        print(f"System Profit Factor: {profit_factor:.2f}")
        print("="*60 + "\n")
        
        if total_trades > 0:
            print("📜 SYSTEM TRANSACTIONS LEDGER AUDIT RECORD (LAST 10 TRADES):")
            for t in self.completed_ledger[-10:]:
                print(f"  {t['exit_date'].strftime('%Y-%m-%d')} | {t['symbol']:15} | "
                      f"₹{t['entry_price']:7.2f} -> ₹{t['exit_price']:7.2f} | "
                      f"PnL: ₹{t['pnl']:+9.2f} | Reason: {t['reason']}")

# =============================================================================
# 4. EXECUTION MATRIX PIPELINE
# =============================================================================
if __name__ == "__main__":
    from data.fyers_client import fyers_client
    
    if not fyers_client.connect():
        logger.error("Database initialization failed.")
    else:
        raw_data = {}
        for idx, sym in enumerate(UNIVERSE):
            try:
                df = fyers_client.get_historical(sym, days=365, resolution="D")
                if df is not None and not df.empty:
                    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
                    raw_data[sym] = df
                import time
                time.sleep(0.12)
            except Exception:
                continue
                
        if raw_data:
            backtester = AuditedInstitutionalBacktester()
            backtester.execute_simulation(raw_data)