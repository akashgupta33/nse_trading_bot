import json
from datetime import datetime
from typing import Optional, Dict, Any
import pytz
from loguru import logger

import anthropic
from config.settings import settings, MarketTime
from indicators.engine import IndicatorSnapshot

IST = pytz.timezone("Asia/Kolkata")

# =============================================================================
# TRADING BRAIN AGENT SUB-SYSTEM TOOL DEFINITIONS (ENTRY LOGIC)
# =============================================================================

TOOLS = [
    {
        "name": "get_indicator_snapshot",
        "description": "Get the full technical and fundamental snapshot for a specific symbol.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_all_scores",
        "description": "Get a ranked summary of all universe stocks with technical and fundamental metrics.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_open_position",
        "description": "Get details of the currently open portfolio matrix layout.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_decision",
        "description": "Submit the final trading entry decision. Call this ONCE at the end.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["buy", "sell", "hold", "no_trade"],
                },
                "symbol": {"type": "string"},
                "reason": {"type": "string"},
                "confidence": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["action", "reason", "confidence"],
        },
    },
]

def build_system_prompt(current_time: datetime, has_open_position: bool) -> str:
    time_str = current_time.strftime("%Y-%m-%d %H:%M")
    h, m = current_time.hour, current_time.minute
    total_min = h * 60 + m

    # Time boundaries derived directly from configuration frameworks
    if total_min < MarketTime.CAUTION_HOUR * 60:
        time_context = "Normal session operations - daily structural entry rules apply."
    elif total_min < MarketTime.NO_ENTRY_HOUR * 60 + MarketTime.NO_ENTRY_MIN:
        time_context = "Afternoon caution phase. Only authorize high-conviction setups."
    else:
        time_context = "Late pre-close window. New trade deployment prohibited."

    position_context = "Active holdings present." if has_open_position else "Portfolio slots available."

    return f"""You are a highly disciplined Lead Portfolio Manager operating on NSE Indian equities.

Current time: {time_str}
Time context: {time_context}
Position context: {position_context}

YOUR DUAL-MANDATE ENTRY STRATEGY:
- Technicals: Must be above 200 SMA, RSI hooked up (40-57), breaking out on 1.1x volume.
- Fundamentals: Cross-reference trailing P/E and Revenue Growth. Reject dying companies. High P/E is only acceptable for high-growth sectors.
- Risk Parity: Capped at 1.5% portfolio equity risk. Targets set dynamically via ATR.

Do NOT catch falling knives. If systemic market decay is detected, submit no_trade."""


class TradingBrain:
    """Claude-powered central trading, cognitive risk, and inflection management brain."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.snapshots: Dict[str, Any] = {}
        self.open_position: Optional[dict] = None
        self.decision: Optional[dict] = None

    # =============================================================================
    # PHASE 2: ENTRY ANALYSIS DESK
    # =============================================================================
    def _execute_tool(self, name: str, inputs: dict) -> str:
        if name == "get_all_scores":
            rows = []
            for sym, snap in sorted(self.snapshots.items(), key=lambda x: getattr(x[1], "screener_score", 0), reverse=True):
                fund = getattr(snap, "fundamentals", {})
                rows.append({
                    "symbol": sym, 
                    "score": snap.screener_score, 
                    "P/E": fund.get("trailingPE", "N/A"), 
                    "Sector": fund.get("sector", "N/A"),
                    "rsi": snap.rsi, 
                    "volume_confirmed": getattr(snap, "volume_confirmed", False)
                })
            return json.dumps(rows, indent=2)
            
        elif name == "get_indicator_snapshot":
            symbol = inputs.get("symbol", "")
            snap = self.snapshots.get(symbol)
            if not snap: 
                return json.dumps({"error": "Symbol not found"})
                
            data = snap.to_dict()
            data["fundamentals"] = getattr(snap, "fundamentals", {})
            return json.dumps(data, indent=2)
            
        elif name == "get_open_position":
            if self.open_position:
                return json.dumps(self.open_position, indent=2)
            else:
                return json.dumps({"message": "No active slots."}, indent=2)
                
        elif name == "submit_decision":
            self.decision = inputs
            return json.dumps({"status": "decision_recorded"})
            
        return json.dumps({"error": f"Unknown tool: {name}"})

    def decide(self, snapshots: dict, open_position: Optional[dict] = None, run_time: Optional[datetime] = None) -> dict:
        self.snapshots = snapshots
        self.open_position = open_position
        self.decision = None
        now = run_time or datetime.now(IST)

        system_prompt = build_system_prompt(now, open_position is not None)
        user_msg = f"Time: {now.strftime('%H:%M')}. Review portfolio and universe candidates to deploy capital optimally."

        messages = [{"role": "user", "content": user_msg}]
        logger.info(f"🧠 [Entry Desk] Analyzing {len(snapshots)} candidates...")

        for _ in range(10):
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022", 
                max_tokens=4000,
                system=system_prompt, 
                tools=TOOLS, 
                messages=messages
            )
            messages.append({"role": "assistant", "content": response.content})
            
            if response.stop_reason == "end_turn":
                break
                
            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result", 
                        "tool_use_id": block.id,
                        "content": self._execute_tool(block.name, block.input)
                    })
                    
            if tool_results: 
                messages.append({"role": "user", "content": tool_results})
                
            if self.decision: 
                break

        if not self.decision:
            self.decision = {
                "action": "no_trade", 
                "reason": "No explicit decision reached.", 
                "confidence": 1
            }
            
        return self.decision

    # =============================================================================
    # PHASE 4: COGNITIVE TRADE MANAGEMENT (CTM) DESKS
    # =============================================================================
    
    def _call_claude_json_prompt(self, prompt: str, desk_name: str) -> dict:
        """Helper method to cleanly request and parse JSON from Claude."""
        try:
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=200,
                temperature=0.1,  
                messages=[{"role": "user", "content": prompt}]
            )
            
            clean_json = response.content[0].text.strip()
            
            if clean_json.startswith("```json"): 
                clean_json = clean_json.split("```json")[1].split("```")[0].strip()
            elif clean_json.startswith("```"): 
                clean_json = clean_json.split("```")[1].split("```")[0].strip()
                
            return json.loads(clean_json)
            
        except Exception as e:
            logger.error(f"Error at {desk_name}: {e}")
            return {}

    def evaluate_inflection_event(self, ctx: dict) -> dict:
        """The Inflection Desk: Handles Greed vs. Logic when Targets hit."""
        logger.info(f"🧠 [Inflection Desk] Analyzing momentum for {ctx.get('symbol')} at {ctx.get('event_type')}...")
        
        fund = ctx.get('fundamentals', {})
        sector = fund.get('sector', 'Unknown')
        pe_ratio = fund.get('trailingPE', 'Unknown')
        
        prompt = f"""
        You are the Lead Portfolio Manager. An active position hit a profit target. Analyze and execute.
        
        EVENT: {ctx.get('event_type')}
        ASSET: {ctx.get('symbol')} | Floating PnL: +{ctx.get('pnl_pct')}%
        FUNDAMENTALS: Sector: {sector} | P/E: {pe_ratio}
        TECHNICALS: Volume is {ctx.get('volume_trend')} | RSI is {ctx.get('current_rsi')}
        
        TARGET 1 OPTIONS:
        1. "SCALE_OUT": Standard momentum. Secure risk by selling 33%.
        2. "HOLD_FULL": Massive volume/growth. Sell nothing. Ride 100% to Target 2.
        3. "EXIT_ALL": Dying volume or reversal. Secure all profit immediately.
        
        TARGET 2 OPTIONS:
        1. "SCALE_OUT_T2": Sell 50% of remaining. Let the rest ride as a tight runner.
        2. "EXIT_ALL": Momentum exhausted or RSI extreme. Take the massive win now.

        Return strictly JSON:
        {{"action": "CHOICE_HERE", "reason": "15 word justification"}}
        """
        
        res = self._call_claude_json_prompt(prompt, "Inflection Desk")
        return res if res else {"action": "SCALE_OUT", "reason": "Fallback scale out due to API parse error."}

    def evaluate_stagnation_risk(self, ctx: dict) -> dict:
        """The Stagnation Desk: Handles Day 21 'Dead Money' opportunity cost."""
        logger.info(f"🧠 [Stagnation Desk] Reviewing capital efficiency for {ctx.get('symbol')}...")
        
        fund = ctx.get('fundamentals', {})
        sector = fund.get('sector', 'Unknown')
        pe_ratio = fund.get('trailingPE', 'Unknown')
        rev_growth = fund.get('revenueGrowth', 'Unknown')
        
        prompt = f"""
        You are the Lead Portfolio Manager. A position has hit our 21-day time limit.
        Is this healthy institutional accumulation, or dead money dragging our capital?
        
        ASSET: {ctx.get('symbol')} | Floating PnL: {ctx.get('pnl_pct')}% | Days Held: {ctx.get('days_held')}
        FUNDAMENTALS: Sector: {sector} | P/E: {pe_ratio} | Rev Growth: {rev_growth}
        TECHNICALS: Volume is {ctx.get('volume_trend')} | RSI is {ctx.get('current_rsi')}
        
        OPTIONS:
        1. "HOLD_EXTEND": Fundamentals are elite, technicals holding support cleanly. Extend time clock by 7 days.
        2. "EXIT_STAGNANT": Weak fundamentals or breaking support. Free up capital for a better slot.

        Return strictly JSON:
        {{"action": "CHOICE_HERE", "reason": "15 word justification"}}
        """
        
        res = self._call_claude_json_prompt(prompt, "Stagnation Desk")
        return res if res else {"action": "EXIT_STAGNANT", "reason": "Fallback time-stop execution."}

    def evaluate_active_position_risk(self, ctx: dict) -> dict:
        """The Risk Desk: Handles 3-day bleeds and downside risk."""
        logger.info(f"🧠 [Risk Desk] Analyzing downside structural integrity for {ctx.get('symbol')}...")

        fund = ctx.get('fundamentals', {})
        sector = fund.get('sector', 'Unknown')
        pe_ratio = fund.get('trailingPE', 'Unknown')

        prompt = f"""
        You are the Lead Portfolio Manager. A position is bleeding prior to hitting the hard math stop.
        Determine if this is a normal retail shakeout or a fundamental institutional breakdown.
        
        ASSET: {ctx.get('symbol')} | Floating PnL: {ctx.get('pnl_pct')}%
        DOWNSIDE METRICS: Dropped {ctx.get('consecutive_down_days')} days in a row.
        VOLUME METRICS: Institutional Heavy Selling detected: {ctx.get('heavy_selling_volume')}
        FUNDAMENTALS: Sector: {sector} | P/E: {pe_ratio}
        
        OPTIONS:
        1. "EXIT_EARLY": Massive distribution volume, structural breakdown, or terrible fundamentals. Kill the trade now to save capital.
        2. "HOLD_TRUST_STOP": Low volume retail drift. The fundamental thesis is intact. Let the mathematical hard stop protect us.

        Return strictly JSON:
        {{"action": "CHOICE_HERE", "reason": "15 word justification"}}
        """

        res = self._call_claude_json_prompt(prompt, "Risk Desk")
        return res if res else {"action": "HOLD_TRUST_STOP", "reason": "Fallback to mathematical hard stop."}


# Unified Singleton Instance Reference Mapping
trading_brain = TradingBrain()