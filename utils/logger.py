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
        self.ready = False
        self._setup()

    def _setup(self):
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.warning("Telegram not configured - alerts disabled")
            return
        try:
            import httpx
            self.ready = True
        except ImportError:
            logger.warning("httpx not installed - Telegram disabled")

    def send(self, message: str):
        if not self.ready:
            logger.info(f"[ALERT-DISABLED] {message.replace('<b>', '').replace('</b>', '')}")
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


class TradeDatabase:
    """SQLite persistence for all trades."""

    def __init__(self):
        import os
        os.makedirs("logs", exist_ok=True)
        self.db_path = getattr(settings, "db_path", "logs/trades.db")
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
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
            with sqlite3.connect(self.db_path) as conn:
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

    def get_daily_pnl(self) -> float:
        """Calculates today's realized PnL from the database."""
        try:
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # Sum all PnL entries logged today
                cursor.execute("""
                    SELECT SUM(pnl) FROM trades 
                    WHERE timestamp LIKE ? AND pnl IS NOT NULL
                """, (f"{today_str}%",))
                result = cursor.fetchone()[0]
                return float(result) if result else 0.0
        except Exception as e:
            logger.error(f"Error calculating daily PnL: {e}")
            return 0.0


# =============================================================================
# EXTENDED PORTFOLIO ALERTER (FOR COGNITIVE DESK SYSTEM)
# =============================================================================

class PortfolioAlerter(TelegramAlerter):
    """Extends TelegramAlerter with CTM AI portfolio-specific messages."""
    
    def alert_scan_start(self):
        """Sends a startup message to Telegram when the EOD pipeline begins."""
        self.send("🔍 <b>System Awake:</b> Initiating End-of-Day Market Scan...")

    def alert_screener_complete(self, top_n: int, bear_market: bool):
        if bear_market:
            self.send("🐻 <b>Macro Screener:</b> Nifty below EMA200. Systemic bear market active. Halting entries.")
        elif top_n == 0:
            self.send("📦 <b>Screener:</b> No stocks survived the institutional Golden Gate today.")
        else:
            self.send(f"🔍 <b>Screener Complete:</b> {top_n} candidates survived technical gates. Routing to Analyst Desk.")

    def alert_watchlist(self, watchlist: list):
        if not watchlist:
            self.send("📋 <b>Analyst Desk:</b> No stocks met the Fundamental Dual-Mandate today. No trades.")
            return
        lines = [f"📋 <b>Analyst High-Conviction Watchlist ({len(watchlist)} stocks):</b>"]
        for ws in watchlist:
            lines.append(
                f"  🔹 <b>{ws.symbol}</b> – Conviction {ws.conviction_score}/10\n"
                f"    Sector: {ws.sector} | Target 2: ₹{ws.target2}\n"
                f"    📝 <i>{ws.thesis[:120]}</i>"
            )
        self.send("\n".join(lines))
        
    def alert_portfolio_decision(self, comment: str, decisions: list):
        """Outputs the final logic of the Execution Manager."""
        msg = f"💼 <b>Portfolio Manager:</b>\n<i>{comment}</i>\n\n"
        if not decisions:
            msg += "No execution actions taken today."
        else:
            for d in decisions:
                action = str(d.get('action', '')).upper()
                sym = d.get('symbol', '')
                qty = d.get('quantity', 0)
                reason = d.get('reason', '')
                msg += f"• <b>{action}</b> {qty}x {sym}\n  <i>{reason}</i>\n"
        self.send(msg)
        
    def alert_claude_execution(self, symbol: str, action: str, reason: str, desk_name: str = "Execution Desk"):
        """Fires when Claude actively intervenes on an exit (Inflection, Stagnation, Risk)."""
        emoji = "🧠"
        if "Scale" in reason or "Profit" in reason: emoji = "💰"
        elif "Risk" in reason or "Exit" in reason: emoji = "🛑"
        elif "Extend" in action or "Hold" in action: emoji = "💎"
        
        self.send(
            f"{emoji} <b>[{desk_name}] Intervention</b>\n"
            f"📈 <b>{symbol}</b> -> Action: <b>{action.upper()}</b>\n"
            f"📝 <i>{reason}</i>"
        )

    def alert_entry(self, trade: dict):
        mode = "PAPER" if trade.get("mode") == "paper" else "LIVE"
        msg = (
            f"🚀 <b>{mode} TRADE ENTRY</b>\n"
            f"📈 <b>{trade.get('symbol')}</b>\n"
            f"💰 Entry: ₹{trade.get('price')}\n"
            f"📦 Qty: {trade.get('qty')} Shares\n"
            f"🛑 Stop Loss: ₹{trade.get('stop_loss')}\n"
            f"🎯 Target 1: ₹{trade.get('target1')}\n"
            f"🎯 Target 2: ₹{trade.get('target2')}\n"
            f"📝 <i>{trade.get('reason', '')[:120]}</i>"
        )
        self.send(msg)

    def alert_exit(self, trade: dict):
        pnl = trade.get("pnl", 0)
        pnl_pct = trade.get("pnl_pct", 0)
        emoji = "🟢" if pnl >= 0 else "🔴"
        mode = "PAPER" if trade.get("mode") == "paper" else "LIVE"
        
        msg = (
            f"{emoji} <b>{mode} TRADE LIQUIDATION</b>\n"
            f"📉 <b>{trade.get('symbol')}</b>\n"
            f"💵 Entry: ₹{trade.get('entry_price')}\n"
            f"🚪 Exit: ₹{trade.get('exit_price')}\n"
            f"📦 Qty Sold: {trade.get('qty')} Shares\n"
            f"💰 Net P&L: ₹{pnl:+.2f} ({pnl_pct:+.2f}%)\n"
            f"📝 Trigger: {trade.get('reason', '')}"
        )
        self.send(msg)

    def alert_eod_report(self, portfolio: list, daily_pnl: float, capital: float):
        emoji = "🟢" if daily_pnl >= 0 else "🔴"
        lines = [
            f"{emoji} <b>Daily Execution Report</b>",
            f"💰 Free Capital: ₹{capital:,.0f} | Daily P&L: ₹{daily_pnl:+.2f}"
        ]
        if portfolio:
            lines.append("\n📊 <b>Active Matrix Holdings:</b>")
            for pos in portfolio:
                pnl = pos.get("unrealised_pnl", 0)
                pnl_e = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"  {pnl_e} <b>{pos['symbol']}</b>: {pos['qty_remaining']} shares |\n"
                    f"    Entry ₹{pos['entry_price']} | P&L ₹{pnl:+.0f}"
                )
        else:
            lines.append("\n🚫 Portfolio Matrix Empty (100% Cash).")
        self.send("\n".join(lines))


# Unified Global Singletons
telegram = PortfolioAlerter()
trade_db = TradeDatabase()
portfolio_alerter = telegram  # Alias to prevent breaking old imports