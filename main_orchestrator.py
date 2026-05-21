import time
from datetime import datetime
import pytz
import yfinance as yf
from loguru import logger

# Configuration and Infrastructure
from config.settings import settings
from data.fyers_client import fyers_client
from execution.order_manager import order_manager
from monitor.alerting import portfolio_alerter

# Agent Brains
from screener.screener import screener_agent
from brain.analyst_agent import analyst_agent
from brain.execution_agent import execution_agent

IST = pytz.timezone("Asia/Kolkata")

def fetch_nse_fundamentals(symbol: str) -> dict:
    """Converts Fyers symbol to Yahoo symbol and fetches live fundamentals."""
    yf_sym = symbol.replace("NSE:", "").replace("-EQ", "") + ".NS"
    try:
        info = yf.Ticker(yf_sym).info
        return {
            "sector": info.get("sector", "Unknown"),
            "trailingPE": round(info.get("trailingPE", 0), 2) if info.get("trailingPE") else "N/A",
            "revenueGrowth": info.get("revenueGrowth", "N/A")
        }
    except Exception as e:
        logger.debug(f"Fundamental fetch failed for {yf_sym}: {e}")
        return {"sector": "Unknown", "trailingPE": "N/A", "revenueGrowth": "N/A"}

def run_afternoon_sentinel():
    """Fires at 3:05 PM IST to check for bleeding positions and execute Target exits."""
    logger.info("🛡️ Initiating Pre-Close Risk Sentinel...")
    
    # The Execution Agent's Sentinel handles routing to Claude's Inflection/Risk/Stagnation desks
    execution_agent.run_intraday_risk_sentinel()
    
    logger.info("✅ Risk Sentinel check complete. System secured.")

def run_end_of_day_routine():
    """Fires at 3:15 PM IST to process new setups and execute portfolio rebalancing."""
    logger.info("🚀 Initiating End-of-Day Institutional Trading Pipeline...")
    portfolio_alerter.alert_scan_start()
    
    # 1. Fetch live open portfolio
    current_portfolio = order_manager.open_positions_dict
    
    # 2. Run the universe screener (Technical Math)
    candidates = screener_agent.run()
    
    # Check if market is in a macro bear trend (candidates will be empty)
    is_bear_market = not getattr(settings, "nifty_above_ema200_required", True) and not candidates
    portfolio_alerter.alert_screener_complete(len(candidates), is_bear_market)
    
    # 3. If zero stocks clear the technical filters, halt safely
    if not candidates:
        logger.warning("No technically qualified setups found today. Analyst phase bypassed.")
        watchlist = []
    else:
        # 4. INJECT FUNDAMENTALS: Attach real-world data to the technical snapshots
        logger.info(f"📊 Fetching live fundamental valuations for {len(candidates)} candidates...")
        for snap in candidates:
            snap.fundamentals = fetch_nse_fundamentals(snap.symbol)
            time.sleep(0.2) # Be polite to Yahoo Finance APIs
            
        # 5. Analyst Agent (Dual-Mandate Review)
        watchlist = analyst_agent.analyse(candidates)
        portfolio_alerter.alert_watchlist(watchlist)
        
    # 6. Fetch live prices for execution decisions
    live_prices = {}
    symbols_to_fetch = set([ws.symbol for ws in watchlist] + list(current_portfolio.keys()))
    
    for sym in symbols_to_fetch:
        try:
            df = fyers_client.get_historical(sym, days=2, resolution="D")
            if df is not None and not df.empty:
                live_prices[sym] = float(df["close"].iloc[-1])
            time.sleep(0.12)
        except Exception as e:
            logger.error(f"Failed to pull execution live quote for {sym}: {e}")
            
    # 7. Execution Agent (Capital Allocation & Entry Sizing)
    final_decisions = execution_agent.decide(
        portfolio=list(current_portfolio.values()),
        watchlist=watchlist,
        live_prices=live_prices
    )
    
    portfolio_alerter.alert_portfolio_decision(final_decisions.get("comment", ""), final_decisions.get("decisions", []))
    
    # 8. Order Manager Execution
    for decision in final_decisions.get("decisions", []):
        sym = decision.get("symbol")
        qty = decision.get("quantity", 0)
        action = decision.get("action")
        reason = decision.get("reason", "No reason provided")
        price = live_prices.get(sym, 0.0)
        
        if action == "buy":
            target_stock = next((s for s in watchlist if s.symbol == sym), None)
            if target_stock:
                order_manager.enter_trade_from_watchlist(
                    symbol=target_stock.symbol,
                    entry_price=price,
                    quantity=qty,
                    stop_loss=target_stock.stop_loss,
                    target1=target_stock.target1,
                    target2=target_stock.target2,
                    target3=target_stock.target3,
                    reason=reason,
                    conviction=target_stock.conviction_score
                )
        
    # 9. Send Daily Summary to Telegram
    daily_pnl = 0.0 # You can query this from your trade_db if you have a helper function
    portfolio_alerter.alert_eod_report(list(order_manager.open_positions_dict.values()), daily_pnl, settings.capital)
    
    logger.info("🏁 End-of-Day Pipeline Successfully Completed.")

if __name__ == "__main__":
    logger.info(f"System Matrix Booted. Trading Mode: {settings.trading_mode.upper()}")