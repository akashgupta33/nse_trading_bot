import json
import math
from datetime import datetime
from typing import Optional, Dict, List, Any
import pytz
import yfinance as yf
import pandas as pd
from loguru import logger

import anthropic
from config.settings import settings

IST = pytz.timezone("Asia/Kolkata")

# =============================================================================
# ASSISTANT SYSTEM PROMPT TEMPLATE MATRIX
# =============================================================================

EXECUTION_SYSTEM_PROMPT = """You are a disciplined portfolio manager running an institutional swing trading book.
You manage a maximum of 4 simultaneous delivery stock positions.

Your capital: ₹{capital:,.0f} ({mode} mode)
Cash reserve: always keep {reserve_pct}% free = ₹{reserve_amount:,.0f}
Max deployable: ₹{deployable:,.0f} (split equally across portfolio slots = ₹{per_position:,.0f} per stock)

YOUR ROLE TODAY:
1. Review the analyst watchlist - is anything fundamentally and technically strong?
2. If you have open slots (< 4 positions), allocate capital sequentially into highest conviction targets.
3. Size positions exactly based on maximum risk thresholds. Do not over-leverage.

ENTRY CRITERIA (all must be true):
- Watchlist stock setup is fully qualified (setup_qualified = True)
- Conviction score >= {min_conviction}
- Current price is within the analyst's entry zone low/high bounds
- Portfolio has room (< 4 positions)

POSITION SIZING (TURTLE RISK PARITY METHOD):
- Quantity = floor({per_position:,.0f} / current_price)
- Always round DOWN - never over-allocate capital bounds
- If quantity == 0 (stock too expensive), skip

PROCESS:
1. get_portfolio() - see all current holdings.
2. get_watchlist() - see today's analyst watchlist recommendations.
3. get_current_prices() - verify current intraday prices before any decision.
4. For each watchlist stock with room in portfolio: decide ENTER or SKIP with reason.
5. submit_decisions() - one call with all your decisions."""

# =============================================================================
# EXECUTION TOOL DICTIONARIES
# =============================================================================

EXECUTION_TOOLS = [
    {
        "name": "get_portfolio",
        "description": "Get all current open portfolio matrix allocations.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_watchlist",
        "description": "Get today's analyst watchlist - stocks recommended for entry.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_current_prices",
        "description": "Get live intraday quotes for specific symbols.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                }
            },
            "required": ["symbols"],
        },
    },
    {
        "name": "submit_decisions",
        "description": "Submit all portfolio decisions for today. Call once with complete list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": ["buy", "no_action"],
                            },
                            "quantity": {
                                "type": "integer",
                                "description": "Shares to act on. 0 for no_action.",
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["symbol", "action", "quantity", "reason"],
                    },
                },
                "portfolio_comment": {
                    "type": "string",
                    "description": "Overall assessment of today's portfolio layout status",
                },
            },
            "required": ["decisions"],
        },
    },
]


class ExecutionAgent:
    """Manages Position Sizing (Entries) and Cognitive Sentinel Operations (Exits)."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.portfolio: List[Any] = []
        self.watchlist: List[Any] = []
        self.live_prices: Dict[str, float] = {}
        self.decisions: List[Dict[str, Any]] = []
        self.comment: str = ""

    def _execute_tool(self, name: str, inputs: dict) -> str:
        if name == "get_portfolio":
            # Safely serialize portfolio objects to dicts if they aren't already
            safe_port = [p.to_dict() if hasattr(p, "to_dict") else p for p in self.portfolio]
            return json.dumps(safe_port, indent=2)
            
        elif name == "get_watchlist":
            safe_watch = [w.to_dict() if hasattr(w, "to_dict") else w for w in self.watchlist]
            return json.dumps(safe_watch, indent=2)
            
        elif name == "get_current_prices":
            symbols = inputs.get("symbols", [])
            result = {sym: self.live_prices.get(sym, 0.0) for sym in symbols}
            return json.dumps(result, indent=2)
            
        elif name == "submit_decisions":
            self.decisions = inputs.get("decisions", [])
            self.comment = inputs.get("portfolio_comment", "")
            return json.dumps({"status": "decisions_recorded", "count": len(self.decisions)})
            
        return json.dumps({"error": f"Unknown tool: {name}"})

    def decide(self, portfolio: list, watchlist: list, live_prices: dict) -> dict:
        """Phase 3: Run Claude execution loop for Capital Allocation (Entries)."""
        self.portfolio = portfolio
        self.watchlist = watchlist
        self.live_prices = live_prices
        self.decisions = []
        self.comment = ""

        capital = settings.capital
        reserve_pct = getattr(settings, "cash_reserve_pct", 5.0)
        reserve_amount = capital * (reserve_pct / 100)
        deployable = capital - reserve_amount
        per_position = deployable / getattr(settings, "max_positions", 4)

        system = EXECUTION_SYSTEM_PROMPT.format(
            capital=capital,
            mode="PAPER" if not getattr(settings, "is_live", False) else "LIVE",
            reserve_pct=reserve_pct,
            reserve_amount=reserve_amount,
            deployable=deployable,
            per_position=per_position,
            min_conviction=getattr(settings, "min_conviction_score", 6),
        )

        n_positions = len(portfolio)
        n_watchlist = len(watchlist)
        
        user_msg = (
            f"Execution session initiated. Portfolio has {n_positions}/4 open positions.\n"
            f"Analyst watchlist has {n_watchlist} stocks ready for processing.\n"
            f"Please execute today's portfolio entry allocations."
        )

        if n_watchlist == 0:
            user_msg += "\nNote: Analyst watchlist is empty - output no_action for entries."

        messages = [{"role": "user", "content": user_msg}]
        logger.info(f"Execution Agent: {n_positions} active slots, {n_watchlist} watchlist suggestions.")

        for iteration in range(12):
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4000,
                system=system,
                tools=EXECUTION_TOOLS,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn": break
            if response.stop_reason != "tool_use": break

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": self._execute_tool(block.name, block.input),
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if self.decisions: break

        if not self.decisions:
            logger.info("Execution Agent finished without explicit action.")
            self.decisions = [{"symbol": "NONE", "action": "no_action", "quantity": 0, "reason": "No entry setups triggered."}]

        return {"decisions": self.decisions, "comment": self.comment}

    # =============================================================================
    # PHASE 4: AUTONOMOUS SENTINEL CIRCUIT BREAKER ENGINE (EXITS)
    # =============================================================================
    
    def _fetch_live_fundamentals(self, symbol: str) -> dict:
        """Helper to fetch quick fundamentals for the Sentinel context."""
        yf_sym = symbol.replace("NSE:", "").replace("-EQ", "") + ".NS"
        try:
            info = yf.Ticker(yf_sym).info
            return {
                "sector": info.get("sector", "Unknown"),
                "trailingPE": round(info.get("trailingPE", 0), 2) if info.get("trailingPE") else "N/A",
                "revenueGrowth": info.get("revenueGrowth", "N/A")
            }
        except Exception:
            return {"sector": "Unknown", "trailingPE": "N/A", "revenueGrowth": "N/A"}

    def run_intraday_risk_sentinel(self):
        """
        Monitors active positions, flags Targets, Stagnation, or Bleeding,
        and routes them to the specialized Claude Brain Review Desks.
        """
        # Imported here to prevent circular dependency at boot
        from execution.order_manager import order_manager
        from data.fyers_client import fyers_client
        from brain.claude_brain import trading_brain
        
        if not order_manager.has_active_positions:
            logger.info("🛡️ [Risk Sentinel] No open portfolio slots active. Standing down.")
            return

        active_positions = order_manager.positions  
        logger.info(f"🛡️ [Risk Sentinel] Scanning structural health models across {len(active_positions)} active holdings...")

        for symbol, pos in list(active_positions.items()):
            try:
                # 1. Standardize attribute parsing safely
                entry_price = getattr(pos, "entry_price", 0.0)
                qty_remaining = getattr(pos, "qty_remaining", 0)
                target1 = getattr(pos, "target1", 0.0)
                target2 = getattr(pos, "target2", 0.0)
                target1_hit = getattr(pos, "target1_hit", False)
                target2_hit = getattr(pos, "target2_hit", False)
                entry_time = getattr(pos, "entry_time", datetime.now(IST))
                
                if entry_price == 0: continue

                # 2. Fetch Context Data
                df = fyers_client.get_historical(symbol, days=30, resolution="D")
                if df is None or df.empty: continue
                
                current_close = float(df["close"].iloc[-1])
                pnl_pct = ((current_close - entry_price) / entry_price) * 100
                current_rsi = df["rsi"].iloc[-1] if "rsi" in df.columns else 65.0
                vol_ma = df["volume"].rolling(20).mean().iloc[-1]
                volume_trend = "Explosive" if df["volume"].iloc[-1] > (vol_ma * 1.5) else "Average/Drying"
                days_held = (datetime.now(IST).date() - entry_time.date()).days
                
                fund = self._fetch_live_fundamentals(symbol)

                # ==========================================
                # EVENT DESK ROUTER
                # ==========================================

                # A. TARGET 2 INFLECTION
                if current_close >= target2 and not target2_hit:
                    ctx = {
                        "event_type": "TARGET_2_HIT", "symbol": symbol, "entry_price": entry_price,
                        "current_price": current_close, "pnl_pct": round(pnl_pct, 2),
                        "volume_trend": volume_trend, "current_rsi": round(current_rsi, 2), "fundamentals": fund
                    }
                    decision = trading_brain.evaluate_inflection_event(ctx)
                    
                    if decision.get("action") == "EXIT_ALL":
                        order_manager.exit_trade_portfolio(symbol, current_close, reason=f"T2 CTM Exit: {decision.get('reason')}")
                    else: # SCALE_OUT_T2
                        scale_qty = math.ceil(qty_remaining * 0.50)
                        order_manager.partial_exit(symbol, current_close, scale_qty, reason=f"T2 CTM Scale: {decision.get('reason')}")
                        pos.target2_hit = True
                        if len(df) >= 2:
                            pos.trailing_stop = float(df["low"].iloc[-2]) # Aggressive runner trail

                # B. TARGET 1 INFLECTION
                elif current_close >= target1 and not target1_hit:
                    ctx = {
                        "event_type": "TARGET_1_HIT", "symbol": symbol, "entry_price": entry_price,
                        "current_price": current_close, "pnl_pct": round(pnl_pct, 2),
                        "volume_trend": volume_trend, "current_rsi": round(current_rsi, 2), "fundamentals": fund
                    }
                    decision = trading_brain.evaluate_inflection_event(ctx)
                    
                    if decision.get("action") == "HOLD_FULL":
                        logger.success(f"💎 [CTM Override] {symbol} momentum massive. Bypassing T1 scale-out.")
                        pos.target1_hit = True
                        pos.trailing_stop = max(getattr(pos, "trailing_stop", 0), entry_price)
                    elif decision.get("action") == "EXIT_ALL":
                        order_manager.exit_trade_portfolio(symbol, current_close, reason=f"T1 CTM Reversal Exit: {decision.get('reason')}")
                    else: # Standard SCALE_OUT
                        scale_qty = math.ceil(qty_remaining * 0.33)
                        order_manager.partial_exit(symbol, current_close, scale_qty, reason=f"T1 CTM Scale: {decision.get('reason')}")
                        pos.target1_hit = True
                        pos.trailing_stop = max(getattr(pos, "trailing_stop", 0), entry_price)

                # C. DAY 21 STAGNATION RISK
                elif days_held >= 21 and pnl_pct < 2.0 and not getattr(pos, "stagnation_reviewed", False):
                    ctx = {
                        "symbol": symbol, "pnl_pct": round(pnl_pct, 2), "days_held": days_held,
                        "volume_trend": volume_trend, "current_rsi": round(current_rsi, 2), "fundamentals": fund
                    }
                    decision = trading_brain.evaluate_stagnation_risk(ctx)
                    
                    if decision.get("action") == "EXIT_STAGNANT":
                        order_manager.exit_trade_portfolio(symbol, current_close, reason=f"Stagnation Desk Exit: {decision.get('reason')}")
                    else: # HOLD_EXTEND
                        logger.info(f"⏳ [CTM Override] Stagnation desk approved 7-day extension for {symbol}.")
                        pos.stagnation_reviewed = True # Prevents daily re-triggering

                # D. DOWNSIDE BLEEDING / RISK DESK
                else:
                    consecutive_drops = 0
                    for i in range(1, min(6, len(df))):
                        if df["close"].iloc[-i] < df["close"].iloc[-i-1]:
                            consecutive_drops += 1
                        else:
                            break

                    heavy_selling = (df["volume"].iloc[-1] > (vol_ma * 1.2)) and (current_close < df["open"].iloc[-1])

                    # Trigger if bleeding occurs before hard stop
                    if consecutive_drops >= 3 or pnl_pct <= -2.5:
                        ctx = {
                            "symbol": symbol, "entry_price": entry_price, "current_price": current_close,
                            "current_pnl_pct": round(pnl_pct, 2), "consecutive_down_days": consecutive_drops,
                            "heavy_selling_volume": heavy_selling, "days_held": days_held, "fundamentals": fund
                        }
                        decision = trading_brain.evaluate_active_position_risk(ctx)
                        
                        if decision.get("action") == "EXIT_EARLY":
                            order_manager.exit_trade_portfolio(symbol, current_close, reason=f"CTM Risk Liquidation: {decision.get('reason')}")
                        else:
                            logger.info(f"🛡️ [Risk Desk] {symbol} downside authorized as normal retail variance. Holding to math stop.")

            except Exception as e:
                logger.error(f"Error executing sentinel loop routing for {symbol}: {e}")
                continue


# Singleton Engine Instance Mapping
execution_agent = ExecutionAgent()