import json
import math
from datetime import datetime
from typing import Optional
import pytz
from loguru import logger

import anthropic
from config.settings import settings
from brain.analyst_agent import WatchlistStock

IST = pytz.timezone("Asia/Kolkata")

# =============================================================================
# ASSISTANT SYSTEM PROMPT TEMPLATE MATRIX
# =============================================================================

EXECUTION_SYSTEM_PROMPT = """You are a disciplined portfolio manager running an institutional swing trading book.
You manage a maximum of 4 simultaneous delivery stock positions.

Your capital: ₹{{capital:,.0f}} ({{mode}} mode)
Cash reserve: always keep {{reserve_pct}}% free = ₹{{reserve_amount:,.0f}}
Max deployable: ₹{{deployable:,.0f}} (split equally across portfolio slots = ₹{{per_position:,.0f}} per stock)

YOUR ROLE TODAY:
1. Review each existing matrix holding - should you exit, hold, or trail the stop?
2. Review the analyst watchlist - is anything better than what you hold?
3. If you have open slots (< 4 positions), allocate capital sequentially into highest conviction targets.
4. Scale out exactly 33% of positions at Target 1, letting the remaining 67% ride toward Target 2 under Chandelier trail rules.

EXIT CRITERIA (any one triggers exit decision):
- Stop loss has been hit (price <= stop_loss_level)
- Trailing floor or breakeven point breached
- Max hold period: 21 days - exit regardless UNLESS position is actively profitable past +2% capital gains (Time-stop extension clause)
- RSI went above 75 (overbought extreme condition)

ENTRY CRITERIA (all must be true):
- Watchlist stock setup is fully qualified (setup_qualified = True)
- Conviction score >= {{min_conviction}}
- Current price is within the analyst's entry zone low/high bounds
- Portfolio has room (< 4 positions)
- Not entering in last 30 minutes of trading (after 3:00 PM IST)

POSITION SIZING (TURTLE RISK PARITY METHOD):
- Quantity = floor({{per_position:,.0f}} / current_price)
- Always round DOWN - never over-allocate capital bounds
- If quantity == 0 (stock too expensive), skip

PROCESS:
1. get_portfolio() - see all current holdings with entry price, current price, P&L, days held
2. get_watchlist() - see today's analyst watchlist recommendations
3. get_current_prices() - verify current intraday prices before any decision
4. For each holding: decide EXIT, HOLD or TRAIL with explicit reason
5. For each watchlist stock with room in portfolio: decide ENTER or SKIP with reason
6. submit_decisions() - one call with all your decisions"""

# =============================================================================
# EXECUTION TOOL DICTIONARIES
# =============================================================================

EXECUTION_TOOLS = [
    {
        "name": "get_portfolio",
        "description": "Get all current open portfolio matrix allocations with entry price, current price, unrealised P&L, days held, stops, and targets.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_watchlist",
        "description": "Get today's analyst watchlist - stocks recommended for entry with conviction scores, entry zones, stops, and investment thesis.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_current_prices",
        "description": "Get live intraday quotes for specific symbols to verify they are in entry zone before placing orders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of NSE symbols to get live prices for",
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
                                "enum": ["buy", "sell", "hold", "sell_partial", "no_action"],
                            },
                            "quantity": {
                                "type": "integer",
                                "description": "Shares to act on. 0 for hold/no_action.",
                            },
                            "reason": {"type": "string"},
                            "new_stop_loss": {
                                "type": "number",
                                "description": "Updated trailing stop for existing positions (optional)",
                            },
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
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.portfolio = []
        self.watchlist = []
        self.live_prices = {}
        self.decisions = []
        self.comment = ""

    def _execute_tool(self, name: str, inputs: dict) -> str:
        if name == "get_portfolio":
            return json.dumps(self.portfolio, indent=2)
        elif name == "get_watchlist":
            return json.dumps([ws.to_dict() for ws in self.watchlist], indent=2)
        elif name == "get_current_prices":
            symbols = inputs.get("symbols", [])
            result = {}
            for sym in symbols:
                price = self.live_prices.get(sym, 0)
                result[sym] = price
            return json.dumps(result, indent=2)
        elif name == "submit_decisions":
            self.decisions = inputs.get("decisions", [])
            self.comment = inputs.get("portfolio_comment", "")
            return json.dumps({"status": "decisions_recorded", "count": len(self.decisions)})
        return json.dumps({"error": f"Unknown tool: {name}"})

    def decide(
        self,
        portfolio: list,
        watchlist: list,
        live_prices: dict,
    ) -> dict:
        """Run Claude execution portfolio management loop."""
        self.portfolio = portfolio
        self.watchlist = watchlist
        self.live_prices = live_prices
        self.decisions = []
        self.comment = ""

        capital = settings.capital
        reserve_pct = settings.cash_reserve_pct
        reserve_amount = capital * reserve_pct / 100
        deployable = capital - reserve_amount
        per_position = deployable / settings.max_positions

        system = EXECUTION_SYSTEM_PROMPT.format(
            capital=capital,
            mode="PAPER" if getattr(settings, "is_live", False) is False else "LIVE",
            reserve_pct=reserve_pct,
            reserve_amount=reserve_amount,
            deployable=deployable,
            per_position=per_position,
            min_conviction=settings.min_conviction_score,
        )

        n_positions = len(portfolio)
        n_watchlist = len(watchlist)
        
        user_msg = (
            f"Execution session initiated. Portfolio has {n_positions}/4 open positions.\n"
            f"Analyst watchlist has {n_watchlist} stocks ready for processing.\n"
            f"Please execute today's portfolio decisions matrix."
        )

        if n_watchlist == 0:
            user_msg += "\nNote: Analyst watchlist is empty - manage active holdings exclusively today."

        messages = [{"role": "user", "content": user_msg}]
        logger.info(f"Execution Agent: {n_positions} active slots, {n_watchlist} watchlist suggestions.")

        for iteration in range(12):
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4046,
                system=system,
                tools=EXECUTION_TOOLS,
                messages=messages,
            )

            logger.debug(f"Execution iteration {iteration+1}: {response.stop_reason}")
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                break
            
            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(f"Execution executing tool: {block.name}")
                    result = self._execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if self.decisions:
                break

        if not self.decisions:
            logger.info("Execution Agent finished without explicit action. Holding all open allocations.")
            self.decisions = [
                {"symbol": "PORTFOLIO", "action": "no_action", "quantity": 0, "reason": "No setups triggered condition thresholds."}
            ]

        logger.info(
            f"Execution decisions finalized:\n" +
            "\n".join(
                f"  {d['action'].upper()} {d.get('quantity', 0)} [{d['symbol']}]: "
                f"{d.get('reason', '')[:80]}"
                for d in self.decisions
            )
        )

        return {"decisions": self.decisions, "comment": self.comment}

    # =============================================================================
    # AUTONOMOUS INTRADAY SENTINEL CIRCUIT BREAKER ENGINE
    # =============================================================================
    def run_intraday_risk_sentinel(self):
        """
        Sentinel execution routine. Monitors live open allocations dynamically,
        intercepting and cutting compounding losses prior to hard stop violations.
        """
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
                # Fetch recent historical pricing vectors (last 10 daily close bars)
                df = fyers_client.get_historical(symbol, days=10, resolution="D")
                if df is None or df.empty:
                    logger.error(f"Unable to fetch historical context matrix streams for {symbol}")
                    continue

                current_close = float(df["close"].iloc[-1])
                pnl_pct = ((current_close - pos.entry_price) / pos.entry_price) * 100

                # 1. High Velocity Momentum Expansion Guard (Capping trailing returns)
                if pnl_pct >= 4.0:
                    current_atr = float(df["high"].iloc[-1] - df["low"].iloc[-1])
                    dynamic_trail_floor = round(current_close - (1.5 * current_atr), 2)
                    
                    if dynamic_trail_floor > pos.trailing_stop:
                        pos.trailing_stop = dynamic_trail_floor
                        logger.success(f"📈 [Sentinel Trailing Up] {symbol} locked profit floor higher at ₹{pos.trailing_stop:.2f}")
                    continue

                # 2. Extract Downward Bleeding Metrics
                consecutive_drops = 0
                for i in range(1, min(6, len(df))):
                    if df["close"].iloc[-i] < df["close"].iloc[-i-1]:
                        consecutive_drops += 1
                    else:
                        break

                # Determine if current block matches heavy institutional liquidations
                vol_ma5 = df["volume"].rolling(window=min(5, len(df))).mean().iloc[-1]
                heavy_selling = (df["volume"].iloc[-1] > (vol_ma5 * 1.2)) and (current_close < df["open"].iloc[-1])

                # Compile clean risk profile payload dictionary
                risk_profile = {
                    "symbol": symbol,
                    "entry_price": pos.entry_price,
                    "current_price": current_close,
                    "current_pnl_pct": round(pnl_pct, 2),
                    "consecutive_down_days": consecutive_drops,
                    "heavy_selling_volume": bool(heavy_selling),
                    "days_held": (datetime.now(IST).date() - pos.entry_time.date()).days
                }

                # 3. Route to Claude Brain risk analysis desk if position decays negatively
                if consecutive_drops >= 3 or pnl_pct <= -2.0:
                    brain_verdict = trading_brain.evaluate_active_position_risk(risk_profile)
                    
                    if brain_verdict.get("action") == "EXIT":
                        logger.warning(f"🚨 [COGNITIVE CIRCUIT BREAKER TRIGGERED] Claude ordered emergency liquidation for {symbol}!")
                        
                        # Command order manager to execute market liquidation order instantly
                        order_manager.exit_trade_portfolio(
                            symbol=symbol, 
                            current_price=current_close, 
                            reason=f"CLAUDE_RISK_OVERRIDE: {brain_verdict.get('reason')}"
                        )
                        
                        # Post emergency transaction data package straight to logging arrays
                        logger.info(f"🛡️ [Sentinel Liquidation Executed] Slot vacated successfully for {symbol}")

            except Exception as e:
                logger.error(f"Error executing sentinel loop parameters for {symbol}: {e}")
                continue


# Singleton Engine Instance Mapping
execution_agent = ExecutionAgent()