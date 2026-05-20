import sqlite3
import json
from datetime import datetime
from typing import Optional
import pytz
from loguru import logger

from config.settings import settings

IST = pytz.timezone("Asia/Kolkata")

class TelegramAlerter:
    """Send trading alerts to Telegram."""

    def __init__(self):
        self.self_bot = None
        self.self_ready = False
        self.self_setup()

    def self_setup(self):
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.warning("Telegram not configured - alerts disabled")
            return
        try:
            import httpx
            self.self_ready = True
        except ImportError:
            logger.warning("httpx not installed - Telegram disabled")

    def send(self, message: str):
        if not self.self_ready:
            logger.info(f"[ALERT-DISABLED] {message}")
            return
        try:
            import httpx
            url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": settings.telegram_chat_id,
                "text": message,
                "parse_mode": "HTML",
            }
            resp = httpx.post(url, json=payload, timeout=5)
            if resp.status_code != 200:
                logger.warning(f"Telegram send failed: {resp.text}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")

    def alert_entry(self, trade: dict):
        mode = "PAPER" if trade.get("mode") == "paper" else "LIVE"
        msg = (
            f"🚀 <b>{mode} TRADE ENTRY</b>\n"
            f"📈 <b>{trade.get('symbol')}</b>\n"
            f"💰 Entry: ₹{trade.get('price')}\n"
            f"📦 Qty: {trade.get('qty')}\n"
            f"🛑 Stop: ₹{trade.get('stop_loss')}\n"
            f"🎯 T1: ₹{trade.get('target1')}\n"
            f"🎯 T2: ₹{trade.get('target2')}\n"
            f"⭐️ Score: {trade.get('entry_score', 'N/A')}/5\n"
            f"📝 {trade.get('reason', '')[:120]}"
        )
        self.send(msg)

    def alert_exit(self, trade: dict):
        pnl = trade.get("pnl", 0)
        emoji = "🟢" if pnl >= 0 else "🔴"
        mode = "PAPER" if trade.get("mode") == "paper" else "LIVE"
        msg = (
            f"{emoji} <b>{mode} TRADE EXIT</b>\n"
            f"📉 <b>{trade.get('symbol')}</b>\n"
            f"💵 Entry: ₹{trade.get('entry_price')}\n"
            f"🚪 Exit: ₹{trade.get('exit_price')}\n"
            f"📦 Qty: {trade.get('qty')}\n"
            f"💰 P&L: ₹{pnl:+.2f} ({trade.get('pnl_pct', 0):+.2f}%)\n"
            f"⏱ Held: {trade.get('hold_time', '2 days')}\n"
            f"📝 Reason: {trade.get('reason', '')}"
        )
        self.send(msg)

    def alert_no_trade(self, reason: str):
        self.send(f"☕️ <b>No trade today</b>\nReason: {reason[:200]}\nCapital preserved in cash.")

    def alert_scan_start(self):
        now = datetime.now(IST).strftime("%H:%M IST")
        self.send(f"🔍 <b>Universe scan started at {now}</b>")

    def alert_error(self, error_str: str):
        self.send(f"⚠️ <b>AGENT ERROR</b>\n{error_str[:300]}")

    def send_daily_report(self, report: dict):
        trades = report.get("trades", 0)
        pnl = report.get("total_pnl", 0)
        wins = report.get("wins", 0)
        losses = report.get("losses", 0)
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{emoji} <b>Daily Report - {report.get('date')}</b>\n\n"
            f"📊 Trades: {trades}\n"
            f"🏆 Wins: {wins} | 📉 Losses: {losses}\n"
            f"💰 Net P&L: ₹{pnl:+.2f}\n"
            f"💳 Capital: ₹{report.get('capital', 0):,.0f}"
        )
        self.send(msg)


class TradeDatabase:
    """SQLite persistence for all trades."""

    def __init__(self):
        import os
        os.makedirs("logs", exist_ok=True)
        self.self_db_path = settings.db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.self_db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT,
                    symbol TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    qty INTEGER,
                    pnl REAL,
                    pnl_pct REAL,
                    stop_loss REAL,
                    target1 REAL,
                    target2 REAL,
                    atr REAL,
                    entry_score INTEGER,
                    reason TEXT,
                    hold_time TEXT,
                    mode TEXT,
                    order_id TEXT,
                    timestamp TEXT,
                    extra_json TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_summary (
                    date TEXT PRIMARY KEY,
                    trades INTEGER,
                    wins INTEGER,
                    losses INTEGER,
                    total_pnl REAL,
                    capital REAL,
                    notes TEXT
                )
            """)
            conn.commit()

    def log_trade(self, trade: dict):
        try:
            with sqlite3.connect(self.self_db_path) as conn:
                conn.execute("""
                    INSERT INTO trades (
                        type, symbol, entry_price, exit_price, qty, pnl, pnl_pct,
                        stop_loss, target1, target2, atr, entry_score,
                        reason, hold_time, mode, order_id, timestamp, extra_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    trade.get("type"),
                    trade.get("symbol"),
                    trade.get("entry_price") or trade.get("price"),
                    trade.get("exit_price"),
                    trade.get("qty"),
                    trade.get("pnl"),
                    trade.get("pnl_pct"),
                    trade.get("stop_loss"),
                    trade.get("target1"),
                    trade.get("target2"),
                    trade.get("atr"),
                    trade.get("entry_score"),
                    trade.get("reason"),
                    trade.get("hold_time"),
                    trade.get("mode", "paper"),
                    trade.get("order_id"),
                    trade.get("timestamp"),
                    json.dumps({k: v for k, v in trade.items() if k not in [
                        "type","symbol","entry_price","exit_price","qty","pnl","pnl_pct"
                    ]})
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"DB log error: {e}")

    def get_daily_pnl(self, date: Optional[str] = None) -> float:
        date = date or datetime.now(IST).strftime("%Y-%m-%d")
        with sqlite3.connect(self.self_db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE type='EXIT' AND DATE(timestamp)=?",
                (date,)
            ).fetchone()
            return round(row[0], 2) if row else 0.0

    def get_all_exits(self, date: Optional[str] = None) -> list:
        date = date or datetime.now(IST).strftime("%Y-%m-%d")
        with sqlite3.connect(self.self_db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE type='EXIT' AND DATE(timestamp)=?",
                (date,)
            ).fetchall()
            return rows


# Singletons
telegram = TelegramAlerter()
trade_db = TradeDatabase()


# ----- Extended alerts for 3-agent portfolio system -----

class PortfolioAlerter(TelegramAlerter):
    """Extends TelegramAlerter with portfolio-specific messages."""

    def alert_screener_complete(self, top_n: int, bear_market: bool):
        if bear_market:
            self.send("🐻 <b>Screener:</b> Nifty below EMA200 – bear market filter active. No new entries today.")
        elif top_n == 0:
            self.send("📦 <b>Screener:</b> No qualifying candidates found today. Capital stays in cash.")
        else:
            self.send(f"🔍 <b>Screener complete:</b> {top_n} candidates passed to analyst.")

    def alert_watchlist(self, watchlist: list):
        if not watchlist:
            self.send("📋 <b>Analyst:</b> No stocks made the watchlist today – no trades.")
            return
        lines = [f"📋 <b>Analyst Watchlist ({len(watchlist)} stocks):</b>"]
        for ws in watchlist:
            lines.append(
                f"  🔹 <b>{ws.symbol}</b> – Conviction {ws.conviction_score}/10\n"
                f"    Entry: ₹{ws.entry_zone_low}-₹{ws.entry_zone_high} |\n"
                f"    Stop: ₹{ws.stop_loss} | T1: ₹{ws.target1}\n"
                f"    📝 <i>{ws.thesis[:120]}</i>"
            )
        self.send("\n".join(lines))

    def alert_portfolio_decision(self, comment: str, decisions: list):
        lines = ["💼 <b>Portfolio Manager Decisions:</b>"]
        for d in decisions:
            action = d.get("action", "").upper()
            symbol = d.get("symbol", "")
            qty = d.get("quantity", 0)
            reason = d.get("reason", "")[:100]
            
            emoji = "⚪️"
            if "BUY" in action: emoji = "🟢"
            elif "SELL" in action: emoji = "🔴"
            elif "HOLD" in action: emoji = "🟡"
            
            if action not in ["NO_ACTION", "HOLD"] or qty > 0:
                lines.append(f"  {emoji} {action} ({qty}) {symbol}\n    ↳ {reason}")
        
        if comment:
            lines.append(f"\n💬 <b>Comment:</b> {comment[:200]}")
        self.send("\n".join(lines))

    def alert_intraday_check(self, triggered: list):
        if not triggered:
            return
        lines = ["⚡️ <b>Intraday Alert:</b>"]
        for t in triggered:
            lines.append(f"  🚨 {t}")
        self.send("\n".join(lines))

    def alert_eod_report(self, portfolio: list, daily_pnl: float, capital: float):
        emoji = "🟢" if daily_pnl >= 0 else "🔴"
        lines = [
            f"{emoji} <b>EOD Report</b>",
            f"💰 Capital: ₹{capital:,.0f} | Daily P&L: ₹{daily_pnl:+.2f}"
        ]
        if portfolio:
            lines.append("\n📈 <b>Open Positions:</b>")
            for pos in portfolio:
                pnl = pos.get("unrealised_pnl", 0)
                pnl_e = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"  {pnl_e} <b>{pos['symbol']}</b>: {pos['qty_remaining']} shares |\n"
                    f"    Entry ₹{pos['entry_price']} |\n"
                    f"    P&L ₹{pnl:+.0f}"
                )
        else:
            lines.append("\n🚫 No open positions - fully in cash.")
        self.send("\n".join(lines))

    def alert_no_trade_today(self, reason: str):
        self.send(f"☕️ <b>No trade today</b>\nReason: {reason[:200]}\nCapital preserved in cash.")

    def alert_stale_exit(self, symbol: str, days: int, gain_pct: float):
        self.send(
            f"⏳ <b>Stale trade exit:</b> {symbol}\n"
            f"Held {days} days with only {gain_pct:+.1f}% gain – exiting to redeploy capital."
        )


# Replace singleton with extended version
portfolio_alerter = PortfolioAlerter()