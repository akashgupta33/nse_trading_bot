import time
from datetime import datetime
import pytz
from loguru import logger

# Configuration and Infrastructure
from config.settings import settings
from data.fyers_client import fyers_client
from execution.order_manager import order_manager

from screener.screener import screener_agent
from brain.analyst_agent import analyst_agent
from brain.execution_agent import execution_agent

IST = pytz.timezone("Asia/Kolkata")

def run_afternoon_sentinel():
    """Fires strictly at 3:05 PM IST to check for bleeding positions and execute emergency exits."""
    logger.info("🛡️ Initiating Pre-Close Risk Sentinel...")
    execution_agent.run_intraday_risk_sentinel()
    logger.info("✅ Risk Sentinel check complete. System secured.")

def run_end_of_day_routine():
    """Fires strictly at 3:15 PM IST to process new setups and execute portfolio rebalancing."""
    logger.info("🚀 Initiating End-of-Day Institutional Trading Pipeline...")
    
    # 1. Fetch live open portfolio dictionary
    current_portfolio = order_manager.open_positions_dict
    
    # 2. Run the universe screener to isolate mathematically qualified setups
    candidates = screener_agent.run()
    
    # 3. If zero stocks clear the strict filters, halt new entries safely
    if not candidates:
        logger.warning("No qualified setups found today. Analyst phase bypassed.")
        watchlist = []
    else:
        # Pass golden candidates to Claude Analyst Agent for structural review
        watchlist = analyst_agent.analyse(candidates)
        
    # 4. Fetch the absolute latest intraday prices for decisions
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
            
    # 5. Execution Agent formulates final risk parity and capital routing decisions
    final_decisions = execution_agent.decide(
        portfolio=list(current_portfolio.values()),
        watchlist=watchlist,
        live_prices=live_prices
    )
    
    # 6. Order Manager transmits payload commands to Fyers / Paper Log
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
        elif action == "sell":
            order_manager.exit_trade_portfolio(sym, price, reason=reason)
        elif action == "sell_partial":
            order_manager.partial_exit(sym, price, qty, reason=reason)
            
    logger.info("🏁 End-of-Day Pipeline Successfully Completed.")


if __name__ == "__main__":
    logger.info(f"System Matrix Booted. Trading Mode: {settings.trading_mode.upper()}")
    
    # For manual testing on your laptop, you can uncomment the two lines below to fire them immediately.
    # For live VPS deployment, keep them commented and use Linux Crontab to trigger these functions at 3:05 PM and 3:15 PM!
    
    # run_afternoon_sentinel()
    # time.sleep(5)
    # run_end_of_day_routine()