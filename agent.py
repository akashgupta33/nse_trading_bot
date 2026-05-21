import math
import sys
import time
from datetime import datetime
from typing import Optional
import pytz
from loguru import logger

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings, MarketTime
from data.fyers_client import fyers_client
from screener.screener import screener_agent
from brain.analyst_agent import analyst_agent, WatchlistStock
from brain.execution_agent import execution_agent
from execution.order_manager import order_manager
from monitor.alerting import portfolio_alerter, trade_db


def job_refresh_auth():
    """Refresh Fyers auth daily at 8:00 AM. Check token first, use Telegram if expired."""
    logger.info("=== JOB: FYERS AUTH REFRESH ===")
    try:
        from auto_auth import verify_token, telegram_auth_request
        from monitor.alerting import portfolio_alerter

        # Check if token is still valid
        if verify_token():
            logger.info("Fyers auth refresh: existing token still valid.")
            portfolio_alerter.send("✅ <b>Fyers token is valid</b> - ready to trade today 🚀")
            return

        logger.warning("Fyers auth refresh: token expired, requesting renewal via Telegram...")
        portfolio_alerter.send(
            "🔑 <b>Daily Token Refresh Needed</b>\n\n"
            "Your Fyers token expired. Please authenticate:\n\n"
            "1️⃣ Check your Telegram in the next 30 seconds\n"
            "2️⃣ Click the login link sent by the bot\n"
            "3️⃣ Enter your OTP + PIN\n"
            "4️⃣ Agent will auto-capture the token\n\n"
            "⏱️ This takes ~30 seconds. Complete before 09:00 for market open."
        )
        
        # Trigger Telegram-based auth (proven to work)
        if telegram_auth_request():
            logger.success("Fyers auth refresh: Telegram-assisted renewal succeeded.")
            portfolio_alerter.send("✅ <b>Token refreshed successfully!</b>\nReady to trade. 🚀")
            return

        logger.error("Fyers auth refresh failed.")
        portfolio_alerter.send(
            "❌ <b>Token Refresh Failed</b>\n\n"
            "Please manually run:\n"
            "`python auto_auth.py --mode telegram`\n\n"
            "Then restart the agent."
        )

    except Exception as e:
        logger.error(f"Fyers auth refresh job error: {e}")
        from monitor.alerting import portfolio_alerter
        portfolio_alerter.send(f"❌ Auth refresh error: {str(e)}")
        portfolio_alerter.send(f"⚠️ Auth refresh error: {str(e)[:150]}")

IST = pytz.timezone("Asia/Kolkata")

# Module-level state - watchlist persists from 9 AM to EOD
_todays_watchlist = []
global_screener_candidates = []
global_todays_watchlist = []

def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    t = h * 60 + m
    return (MarketTime.MARKET_OPEN_HOUR * 60 + MarketTime.MARKET_OPEN_MIN) <= t <= (MarketTime.MARKET_CLOSE_HOUR * 60 + MarketTime.MARKET_CLOSE_MIN)


# ############################################################################
# JOB 1 - Screener (8:45 AM)
# ############################################################################

def job_screener():
    """Scan NSE universe and pick top 20 candidates."""
    logger.info("=== JOB: SCREENER (8:45 AM) ===")
    try:
        candidates = screener_agent.run()
        
        if candidates is None:
            portfolio_alerter.send("⚠ Screener failed to run - check Fyers connection")
            return
            
        if len(candidates) == 0:
            portfolio_alerter.send_screener_complete(0, bear_market=True)
            return
            
        portfolio_alerter.send_screener_complete(len(candidates), bear_market=False)
        logger.info(f"Screener passed {len(candidates)} candidates to analyst")
        
        global global_screener_candidates
        global_screener_candidates = candidates
        
    except Exception as e:
        logger.error(f"Screener job error: {e}")
        portfolio_alerter.send(f"⚠ Screener job error: {str(e)[:200]}")


# ############################################################################
# JOB 2 - Analyst Agent (9:00 AM)
# ############################################################################

def job_analyst():
    """Claude analyses screener candidates -> watchlist."""
    logger.info("=== JOB: ANALYST AGENT (9:00 AM) ===")
    global global_screener_candidates, global_todays_watchlist
    
    if not global_screener_candidates:
        logger.info("No screener candidates -> analyst skipped")
        global_todays_watchlist = []
        portfolio_alerter.send_no_trade_today("Screener found no qualifying candidates.")
        return
        
    try:
        watchlist = analyst_agent.analyse(global_screener_candidates)
        global_todays_watchlist = watchlist
        portfolio_alerter.send_watchlist(watchlist)
        
        if not watchlist:
            logger.info("Analyst: empty watchlist - no stocks qualify today")
            
    except Exception as e:
        logger.error(f"Analyst job error: {e}")
        portfolio_alerter.send(f"⚠ Analyst Agent error: {str(e)[:200]}")
        global_todays_watchlist = []


# ############################################################################
# JOB 3 - Execution Agent (9:20 AM)
# ############################################################################

def job_execution():
    """Claude portfolio review -> actual trades."""
    logger.info("=== JOB: EXECUTION AGENT (9:20 AM) ===")
    global global_todays_watchlist
    
    try:
        # Get current portfolio state
        portfolio = [pos.to_dict() for pos in order_manager.self_positions.values()]
        
        # Enrich portfolio with current prices and unrealised P&L
        for pos_dict in portfolio:
            quote = fyers_client.get_quote(pos_dict["symbol"])
            if quote:
                ltp = quote.get("ltp", pos_dict["entry_price"])
                pos_dict["current_price"] = ltp
                pos_dict["unrealised_pnl"] = round((ltp - pos_dict["entry_price"]) * pos_dict["qty_remaining"])
                pos_dict["unrealised_pct"] = round(((ltp - pos_dict["entry_price"]) / pos_dict["entry_price"]) * 100, 2)
                
                entry_time = datetime.fromisoformat(pos_dict["entry_time"])
                pos_dict["days_held"] = (datetime.now(IST) - entry_time.replace(tzinfo=IST)).days
            else:
                pos_dict["current_price"] = pos_dict["entry_price"]
                pos_dict["unrealised_pnl"] = 0
                pos_dict["unrealised_pct"] = 0
                pos_dict["days_held"] = 0
                
        # Get live prices for watchlist stocks
        live_prices = {}
        for ws in global_todays_watchlist:
            quote = fyers_client.get_quote(ws.symbol)
            if quote:
                live_prices[ws.symbol] = quote
                
        result = execution_agent.decide(portfolio, global_todays_watchlist, live_prices)
        decisions = result.get("decisions", [])
        comment = result.get("comment", "")
        
        portfolio_alerter.alert_portfolio_decision(comment, decisions)
        _execute_decisions(decisions, live_prices)
        
    except Exception as e:
        logger.error(f"Execution job error: {e}")
        portfolio_alerter.send(f"⚠ Execution Agent error: {str(e)[:200]}")


def _execute_decisions(decisions: list, live_prices: dict):
    """Route Claude's decisions to the order manager."""
    for decision in decisions:
        action = decision.get("action", "").lower()
        symbol = decision.get("symbol")
        quantity = int(decision.get("quantity", 0))
        reason = decision.get("reason", "")
        new_stop = decision.get("new_stop_loss")
        
        if action == "no_action" or action == "hold":
            if new_stop and symbol in order_manager.self_positions:
                pos = order_manager.self_positions[symbol]
                if new_stop > pos.trailing_stop:
                    pos.trailing_stop = new_stop
                    logger.info(f"Trailing stop updated: {symbol} -> {new_stop}")
            continue
            
        elif action == "buy" and quantity > 0:
            # Find the watchlist entry for this symbol
            global global_todays_watchlist
            ws = next((w for w in global_todays_watchlist if w.symbol == symbol), None)
            price = live_prices.get(symbol, {}).get("ltp", 0)
            
            if not ws or not price:
                logger.warning(f"Cannot buy {symbol} - no watchlist entry or price")
                continue
                
            # Build a minimal snapshot-like dict for order_manager
            record = order_manager.enter_trade_from_watchlist(
                symbol=symbol,
                entry_price=price,
                quantity=quantity,
                stop_loss=ws.stop_loss,
                target1=ws.target1,
                target2=ws.target2,
                target3=ws.target3,
                reason=reason,
                conviction=ws.conviction_score,
            )
            if record:
                trade_db.log_trade(record)
                portfolio_alerter.alert_entry(record)
                
        elif action in ["sell", "sell_partial"] and quantity > 0:
            quote = live_prices.get(symbol)
            price = quote.get("ltp") if quote else 0
            if not price:
                quote = fyers_client.get_quote(symbol)
                price = quote.get("ltp") if quote else 0
                
            if action == "sell":
                record = order_manager.exit_trade(price, reason)
                if record:
                    trade_db.log_trade(record)
                    portfolio_alerter.alert_exit(record)
            elif action == "sell_partial":
                record = order_manager.partial_exit(price, quantity, reason)
                if record:
                    trade_db.log_trade(record)
                    portfolio_alerter.alert_partial_exit(record)


# ############################################################################
# JOB 4 - Intraday Monitor (every 30 min, 10 AM-3 PM)
# ############################################################################

def job_intraday_monitor():
    """Check open positions for stop-loss hits and significant moves."""
    if not order_manager.self_positions:
        return
        
    now = datetime.now(IST)
    h, m = now.hour, now.minute
    t = h * 60 + m
    start = MarketTime.INTRADAY_CHECK_START[0] * 60 + MarketTime.INTRADAY_CHECK_START[1]
    end = MarketTime.INTRADAY_CHECK_END[0] * 60 + MarketTime.INTRADAY_CHECK_END[1]
    
    if not (start <= t <= end):
        return
        
    triggered_alerts = []
    
    for symbol, pos in list(order_manager.self_positions.items()):
        quote = fyers_client.get_quote(symbol)
        if not quote:
            continue
        ltp = quote.get("ltp", pos.entry_price)
        
        # Stop loss check
        effective_stop = max(pos.trailing_stop, pos.stop_loss)
        if ltp <= effective_stop:
            logger.info(f"STOP LOSS HIT: {symbol} @ ₹{ltp} (stop ₹{effective_stop})")
            record = order_manager.exit_trade(ltp, reason="stop_loss_hit_intraday")
            if record:
                trade_db.log_trade(record)
                portfolio_alerter.alert_exit(record)
            continue
            
        # Target 1 check
        if not pos.target1_hit and ltp >= pos.target1:
            half_qty = pos.pos_qty_remaining // 2
            if half_qty > 0:
                record = order_manager.partial_exit(ltp, half_qty, reason="target1_hit")
                if record:
                    trade_db.log_trade(record)
                    portfolio_alerter.alert_partial_exit(record)
                    triggered_alerts.append(
                        f"🎯 T1 hit: {symbol} @ ₹{ltp} | "
                        f"Sold {half_qty} | Stop moved to ₹{pos.entry_price}"
                    )
            continue
            
        # Large intraday move alert only (Claude not called to save cost)
        move_pct = abs(ltp - pos.entry_price) / pos.entry_price * 100
        if move_pct >= settings.intraday_cl_ude_trigger_pct:
            direction = "🔺" if ltp > pos.entry_price else "🔻"
            triggered_alerts.append(
                f"{direction} Large move: {symbol} ({move_pct:+.1f}%) today (₹{ltp})"
            )
            
        # Stale trade check
        entry_time = pos.entry_time
        days_held = (datetime.now(IST) - entry_time).days
        gain_pct = (ltp - pos.entry_price) / pos.entry_price * 100
        if days_held >= settings.stale_trade_days and gain_pct < settings.stale_trade_min_gain_pct:
            logger.info(f"STALE TRADE: {symbol} held {days_held}d with {gain_pct:.1f}% gain")
            record = order_manager.exit_trade(ltp, reason="stale_trade")
            if record:
                trade_db.log_trade(record)
                portfolio_alerter.alert_stale_exit(symbol, days_held, gain_pct)
                
    if triggered_alerts:
        portfolio_alerter.alert_intraday_check(triggered_alerts)


# ############################################################################
# JOB 5 - EOD Review (3:15 PM)
# ############################################################################

def job_eod_review():
    """Update trailing stops. Send EOD P&L report."""
    logger.info("=== JOB: EOD REVIEW (3:15 PM) ===")
    
    portfolio_list = []
    for symbol, pos in order_manager.self_positions.items():
        quote = fyers_client.get_quote(symbol)
        ltp = quote.get("ltp", pos.entry_price) if quote else pos.entry_price
        
        # Trail stop: If position is up >10%, move stop to breakeven
        gain_pct = ((ltp - pos.entry_price) / pos.entry_price) * 100
        if gain_pct >= 10.0 and pos.trailing_stop < pos.entry_price:
            pos.trailing_stop = pos.entry_price
            logger.info(f"Trailing stop moved to breakeven: {symbol} @ ₹{pos.entry_price}")
            
        # Trail stop: If up >20%, lock in 10% gain
        if gain_pct >= 20.0:
            lock_stop = round(pos.entry_price * 1.10, 2)
            if pos.trailing_stop < lock_stop:
                pos.trailing_stop = lock_stop
                logger.info(f"Trailing stop locked at +10%: {symbol} @ ₹{lock_stop}")
                
        pos_dict = pos.to_dict()
        pos_dict["current_price"] = ltp
        pos_dict["unrealised_pnl"] = round((ltp - pos.entry_price) * pos.qty_remaining, 2)
        portfolio_list.append(pos_dict)
        
    daily_pnl = order_manager.get_daily_pnl()
    portfolio_alerter.alert_eod_report(portfolio_list, daily_pnl, settings.capital)


# ############################################################################
# Entry points
# ############################################################################

def run_now():
    """Manual trigger - runs full pipeline immediately for testing."""
    logger.info("Manual run triggered")
    if not fyers_client.connect():
        logger.error("Fyers not connected. Run python auth.py")
        return
    job_screener()
    time.sleep(2)
    job_analyst()
    time.sleep(2)
    job_execution()


def start_scheduler():
    logger.info("Starting Portfolio Agent scheduler...")
    
    if not fyers_client.connect():
        logger.error("Fyers not connected.")
        return
        
    scheduler = BlockingScheduler(timezone=IST)
    
    scheduler.add_job(job_refresh_auth, CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=IST), id="auth_refresh")
    scheduler.add_job(job_screener, CronTrigger(day_of_week="mon-fri", hour=8, minute=45, timezone=IST), id="screener")
    scheduler.add_job(job_analyst, CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=IST), id="analyst")
    scheduler.add_job(job_execution, CronTrigger(day_of_week="mon-fri", hour=9, minute=20, timezone=IST), id="execution")
    scheduler.add_job(job_eod_review, CronTrigger(day_of_week="mon-fri", hour=15, minute=15, timezone=IST), id="eod_review")
    
    scheduler.add_job(
        job_intraday_monitor,
        IntervalTrigger(minutes=settings.intraday_check_interval_min),
        id="intraday_monitor",
        max_instances=1,
    )
    
    logger.success(
        "Scheduler ready:\n"
        "  08:45  Screener\n"
        "  09:00  Analyst Agent (Claude)\n"
        "  09:20  Execution Agent (Claude)\n"
        f"  Every {settings.intraday_check_interval_min} min Intraday monitor\n"
        "  15:15  EOD review"
    )
    
    portfolio_alerter.send("🚀 Portfolio agent started. Monitoring automated.")
    
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        run_now()
    else:
        start_scheduler()