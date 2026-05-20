import json
from datetime import datetime
from typing import Optional
import pytz
from loguru import logger

import anthropic
from config.settings import settings, MarketTime
from indicators.engine import IndicatorSnapshot

IST = pytz.timezone("Asia/Kolkata")

# =============================================================================
# TRADING BRAIN AGENT SUB-SYSTEM TOOL DEFINITIONS
# =============================================================================

TOOLS = [
    {
        "name": "get_indicator_snapshot",
        "description": (
            "Get the full technical indicator snapshot for a specific symbol "
            "from the pre-computed universe data. Returns all indicators, "
            "entry score, stop loss, targets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "NSE symbol e.g. NSE:BEL-EQ",
                }
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_all_scores",
        "description": (
            "Get a ranked summary of all universe stocks with their entry scores "
            "(0-5), RSI, ADX, volume ratio, and R:R. Use this to identify the "
            "best candidate before deep-diving into one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_open_position",
        "description": "Get details of the currently open positions matrix layout.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "submit_decision",
        "description": (
            "Submit the final trading decision. Call this ONCE at the end "
            "after completing your analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["buy", "sell", "hold", "no_trade"],
                    "description": (
                        "buy = enter new position, sell = exit open position, "
                        "hold = keep existing position, no_trade = no action today"
                    ),
                },
                "symbol": {
                    "type": "string",
                    "description": "Symbol to act on (required for buy/sell).",
                },
                "reason": {
                    "type": "string",
                    "description": "Detailed reasoning for this decision (2-3 sentences).",
                },
                "confidence": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Confidence score 1-10.",
                },
                "entry_score": {
                    "type": "integer",
                    "description": "The entry condition score (0-5) for the chosen stock.",
                },
            },
            "required": ["action", "reason", "confidence"],
        },
    },
]

# =============================================================================
# SYSTEM PROMPT ARCHITECTURE DEFINITIONS
# =============================================================================

def build_system_prompt(current_time: datetime, has_open_position: bool) -> str:
    time_str = current_time.strftime("%Y-%m-%d %H:%M")
    h, m = current_time.hour, current_time.minute
    total_min = h * 60 + m

    # Time boundaries derived directly from configuration frameworks
    if total_min < 13 * 60:  # Before 1:00 PM IST
        time_context = "Normal session operations - daily structural entry rules apply (R:R >= 2.0)."
    elif total_min < 14 * 60 + 45:  # Before 2:45 PM IST
        time_context = "Afternoon caution phase. Only authorize new entries if confirmation setup score is 5 and R:R >= 3.0."
    else:
        time_context = "Late pre-close window (after 2:45 PM IST). New trade deployment prohibited. Focus exclusively on risk mitigation."

    position_context = (
        "Active holdings are present in the portfolio matrix. Review exit parameters before processing deployment requests."
        if has_open_position
        else "Portfolio slots available. You are scanning the screener for institutional trend resumption pullbacks."
    )

    return f"""You are a highly disciplined algorithmic trading agent operating on liquid NSE Indian equities.

Current time: {time_str}
Time context: {time_context}
Position context: {position_context}

YOUR SWING STRATEGY RULES:
- You select entries strictly from the curated high-volume watchlist.
- Simultaneous allocation across up to 4 parallel portfolio slots is allowed.
- Entry requires ALL confirmation filters passed (setup_qualified = True):
  1. macro_bullish: Daily Close above 200 SMA baseline.
  2. rsi_in_zone: Daily RSI pulled back neatly within the 40.0 - 57.0 pool.
  3. rsi_turning_up: Today's RSI hooked higher than yesterday's RSI (Bleeding halted).
  4. price_breaking_up: Daily close strictly higher than yesterday's high candle.
  5. volume_confirmed: Volume backed by institutional size (At least 1.1x of the 20-day Volume MA).

RISK EXECUTIONS:
- Position Size = Calculated dynamically via 1.5% total portfolio equity risk parity rules.
- Stop Loss Floor = Entry price minus (2.5 * ATR).
- Target 1 (Shave 33% shares) = Entry price plus (2.0 * ATR).
- Target 2 (Liquidate Remaining 67%) = Entry price plus (3.0 * ATR).

CRITICAL CONSTRAINTS:
- Do NOT catch a falling knife. If a stock does not show a positive price high and RSI breakout hook, skip it.
- If market context indicates macro-index systemic decay, submit no_trade to protect capital."""


class TradingBrain:
    """Claude-powered central trading and cognitive risk mitigation brain."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.snapshots = {}         
        self.open_position = None   
        self.decision = None

    def _execute_tool(self, name: str, inputs: dict) -> str:
        """Dispatch tool calls from Claude to Python functions."""
        if name == "get_all_scores":
            return self._tool_get_all_scores()
        elif name == "get_indicator_snapshot":
            symbol = inputs.get("symbol", "")
            return self._tool_get_indicator_snapshot(symbol)
        elif name == "get_open_position":
            return self._tool_get_open_position()
        elif name == "submit_decision":
            self.decision = inputs
            return json.dumps({"status": "decision_recorded", "action": inputs.get("action")})
        return json.dumps({"error": f"Unknown tool: {name}"})

    def _tool_get_all_scores(self) -> str:
        rows = []
        for sym, snap in sorted(
            self.snapshots.items(),
            key=lambda x: (getattr(x[1], "setup_qualified", False), x[1].screener_score),
            reverse=True,
        ):
            rows.append({
                "symbol": sym,
                "setup_qualified": getattr(snap, "setup_qualified", False),
                "score": snap.entry_score,
                "screener_score": snap.screener_score,
                "close": snap.close,
                "rsi": snap.rsi,
                "rsi_turning_up": getattr(snap, "rsi_turning_up", False),
                "price_breaking_up": getattr(snap, "price_breaking_up", False),
                "volume_confirmed": getattr(snap, "volume_confirmed", False),
                "adx": snap.adx,
                "supertrend": "BULL" if snap.supertrend_bullish else "BEAR",
                "volume_ratio": snap.volume_ratio,
                "rr_ratio": snap.rr_ratio,
                "stop_loss": snap.stop_loss_price,
                "target1": snap.target1_price,
                "target2": snap.target2_price,
            })
        return json.dumps(rows, indent=2)

    def _tool_get_indicator_snapshot(self, symbol: str) -> str:
        snap = self.snapshots.get(symbol)
        if not snap:
            available = list(self.snapshots.keys())
            return json.dumps({"error": f"Symbol '{symbol}' not in universe. Available: {available}"})
        return json.dumps(snap.to_dict(), indent=2)

    def _tool_get_open_position(self) -> str:
        if not self.open_position:
            return json.dumps({"open_position": None, "message": "No active open portfolio slots found."})
        return json.dumps(self.open_position, indent=2)

    def decide(
        self,
        snapshots: dict,
        open_position: Optional[dict] = None,
        run_time: Optional[datetime] = None,
    ) -> Optional[dict]:
        """Run Claude's agentic loop and return a trading decision."""
        self.snapshots = snapshots
        self.open_position = open_position
        self.decision = None

        now = run_time or datetime.now(IST)
        has_position = open_position is not None

        system_prompt = build_system_prompt(now, has_position)

        h, m = now.hour, now.minute
        late = (h * 60 + m) >= 14 * 60 + 45  # After 2:45 PM IST

        if late:
            user_msg = (
                "Market is approaching the daily closing bell.\n"
                "Only examine risk mitigation parameters on active positions.\n"
                "Do NOT open new trades."
            )
        elif has_position:
            user_msg = (
                f"Current execution timestamp: {now.strftime('%H:%M')}.\n"
                f"Active holdings found. Audit for any exit signals first, "
                f"then scan the universe for superior capital efficiency setups."
            )
        else:
            user_msg = (
                f"Current execution timestamp: {now.strftime('%H:%M')}.\n"
                "Portfolio slots vacant. Scan the watch baskets for confirmed "
                "pullback trend resumptions."
            )

        messages = [{"role": "user", "content": user_msg}]
        logger.info(f"Claude brain execution initialized - {len(snapshots)} symbols, active_holding={has_position}")

        max_iterations = 10
        for iteration in range(max_iterations):
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4000,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            logger.debug(f"Claude iteration {iteration+1}: stop_reason={response.stop_reason}")
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(f"Brain executing tool: {block.name}({json.dumps(block.input)[:120]})")
                    result_str = self._execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if self.decision:
                break

        if not self.decision:
            logger.warning("Claude completed sequence without explicit action submittal. Falling back to no_trade.")
            self.decision = {
                "action": "no_trade",
                "symbol": None,
                "reason": "Iteration loop completed without explicit payload submission.",
                "confidence": 1,
                "entry_score": 0,
            }

        return self.decision

    # =============================================================================
    # COGNITIVE RISK AND SENTINEL CIRCUIT BREAKER DESK
    # =============================================================================
    def evaluate_active_position_risk(self, risk_profile: dict) -> dict:
        """
        Autonomous risk analysis core. Determines if an underwater asset is undergoing
        a normal healthy pullback or systematic institutional breakdown.
        """
        logger.info(f"🧠 [Claude Brain] Running quantitative risk inspection for {risk_profile['symbol']}...")

        prompt = f"""
        You are an institutional risk manager supervising an automated delivery swing trading portfolio.
        Analyze the following technical decay context of an active position:
        
        POSITION METRICS:
        - Ticker Symbol: {risk_profile['symbol']}
        - Entry Purchase Price: ₹{risk_profile['entry_price']}
        - Current Market Price: ₹{risk_profile['current_price']}
        - Realized Floating PnL: {risk_profile['current_pnl_pct']}%
        - Consecutive Negative Closes: {risk_profile['consecutive_down_days']} days
        - Institutional Volume Liquidation Spike: {risk_profile['heavy_selling_volume']}
        - Days Transacted in Portfolio: {risk_profile['days_held']} days

        RISK ARCHITECTURE LAWS:
        1. If the asset has dropped for 3+ days consecutively AND the closing price is falling on heavier volume (heavy_selling_volume = True), this indicates institutional distribution. Exit immediately.
        2. If the current floating loss exceeds -2.5% and the price action shows zero reversal candle attempts on the daily chart, exit immediately to protect capital.
        3. If the position is moving sideways on low, dry retail volume, return HOLD to allow the structural ATR floor to function.

        You must respond with a strictly formatted JSON object. Do not include any conversational text, markdown strings, or block enclosures.
        RESPONSE TEMPLATE:
        {{
            "action": "EXIT" or "HOLD",
            "reason": "Provide a concise analytical justification under 12 words"
        }}
        """

        try:
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=150,
                temperature=0.0,  # Pure mathematical discipline
                messages=[{"role": "user", "content": prompt}]
            )
            
            clean_json = response.content[0].text.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json.split("```json")[1].split("```")[0].strip()
            elif clean_json.startswith("```"):
                clean_json = clean_json.split("```")[1].split("```")[0].strip()

            result = json.loads(clean_json)
            logger.warning(f"🧠 [Brain Risk Decision] Analysis completed for {risk_profile['symbol']}: Action={result['action']} | Reason={result['reason']}")
            return result

        except Exception as e:
            logger.error(f"Critical error parsing brain's risk decision payload: {e}. Defaulting to safe portfolio HOLD.")
            return {"action": "HOLD", "reason": "System exception error fallback parsing core state."}


# Unified Singleton Instance Reference Mapping
trading_brain = TradingBrain()