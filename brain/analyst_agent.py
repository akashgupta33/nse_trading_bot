import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from loguru import logger

import anthropic
from config.settings import settings

@dataclass
class WatchlistStock:
    """A stock selected by the Analyst Agent for the Execution Agent."""
    symbol: str
    conviction_score: float = 0.0     # 1-10, Claude's confidence
    entry_zone_low: float = 0.0      # buy anywhere in this range
    entry_zone_high: float = 0.0
    stop_loss: float = 0.0           # below pullback low
    target1: float = 0.0             # first profit target
    target2: float = 0.0             # second profit target
    target3: float = 0.0             # let-it-run target
    thesis: str = ""                 # 2-3 sentence investment case
    risk_factors: str = ""           # what could go wrong
    sector: str = ""
    hold_days_estimate: int = 20     # expected hold period
    setup_type: str = ""             # "pullback_at_ema20" / "pullback_at_ema50" etc.
    screener_score: float = 0.0
    screener_rank: int = 0
    current_price: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

# =============================================================================
# ANALYST AGENT SUB-SYSTEM TOOL DEFINITIONS
# =============================================================================

ANALYST_TOOLS = [
    {
        "name": "get_candidate_list",
        "description": (
            "Get ranked summary of all screener candidates with key metrics: "
            "screener score, RSI, pullback %, pullback days, ADX, "
            "EMA support levels, entry candle signal, and R:R ratio. "
            "Call this first to see the full list."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_stock_detail",
        "description": (
            "Get the complete technical snapshot for a specific stock. "
            "Includes all indicator values, pullback analysis, "
            "support levels, and suggested entry/stop/target levels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "NSE symbol e.g. NSE:RELIANCE-EQ",
                }
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "submit_watchlist",
        "description": (
            "Submit your final watchlist of highest conviction stocks. "
            "Call once after completing your analysis. "
            "Only include stocks you genuinely believe have strong setups."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "watchlist": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "conviction_score": {"type": "number", "minimum": 1, "maximum": 10},
                            "entry_zone_low": {"type": "number"},
                            "entry_zone_high": {"type": "number"},
                            "stop_loss": {"type": "number"},
                            "target1": {"type": "number"},
                            "target2": {"type": "number"},
                            "target3": {"type": "number"},
                            "thesis": {"type": "string"},
                            "risk_factors": {"type": "string"},
                            "sector": {"type": "string"},
                            "hold_days_estimate": {"type": "integer"},
                            "setup_type": {"type": "string"},
                        },
                        "required": [
                            "symbol", "conviction_score", "entry_zone_low", 
                            "entry_zone_high", "stop_loss", "target1", "target2", "thesis"
                        ],
                    },
                    "minItems": 0,
                    "maxItems": 5,
                    "description": "Selected stocks. Submit empty list if nothing meets the bar.",
                }
            },
            "required": ["watchlist"],
        },
    },
]

ANALYST_SYSTEM_PROMPT = """You are a senior equity analyst at a top Indian fund house.
Your job: review screener candidates and identify 1-5 stocks with the strongest
pullback-in-trend setups for swing trading delivery positions (holding 2-6 weeks).

STRATEGY YOU ARE LOOKING FOR - Institutional Pullback:
- Stock is in a clear uptrend (above EMA200, EMA50 slope rising)
- Stock has pulled back 3-15% from its recent high over 3-12 days
- Volume dried up during the pullback (sellers exhausted)
- Today shows an unmitigated institutional resumption signal: price breaking past previous high with dynamic 1.1x volume confirmation
- RSI pulled back into the 40-57 zone and has hooked upwards securely (not in free fall)

CONVICTION SCORING GUIDE (1-10):
9-10: All volume, direction, and structural confirmation gates clear. No trailing decay. Real sector tailwind.
7-8: Most signals aligned, minor tracking consolidation but overall risk profile remains highly premium.
5-6: Mixed signals - include only if structural price action confirmation completely bypasses the decay.
Below 5: Do NOT include under any circumstances.

WHAT YOU MUST CHECK FOR EACH CANDIDATE:
1. Is the macro trend genuinely intact above the 200 EMA baseline?
2. Is the pullback healthy or is it an unhedged cascading breakdown?
3. Are the directional hooks on price high and RSI confirmed turning up?
4. Entry zone low and high must align neatly within a +/- 1% range of current close.
5. Stop loss goes safely below the pullback structure floor (calculated via 2.5 * ATR below close).
6. Target 1 (1:1 R:R), Target 2 (Extended structural swing target zone 3.0 * ATR), and Target 3 must map cleanly.
7. What is the R:R? Minimum 2:1 required on Target 2 calculations.
8. What could go wrong? State structural invalidation factors clearly.

PROCESS:
1. Call get_candidate_list() to see all candidates ranked by screener score.
2. Deep-dive into top candidates using get_stock_detail().
3. Skip any stock failing the strict confirmation rules.
4. Call submit_watchlist() with chosen stocks. Empty is completely acceptable if nothing qualifies."""


class AnalystAgent:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.candidates = []
        self.watchlist = []

    def _execute_tool(self, name: str, inputs: dict) -> str:
        if name == "get_candidate_list":
            return self._tool_candidate_list()
        elif name == "get_stock_detail":
            return self._tool_stock_detail(inputs.get("symbol", ""))
        elif name == "submit_watchlist":
            self.watchlist = inputs.get("watchlist", [])
            return json.dumps({"status": "watchlist_recorded", "count": len(self.watchlist)})
        return json.dumps({"error": f"Unknown tool: {name}"})

    def _tool_candidate_list(self) -> str:
        rows = []
        for snap in self.candidates:
            rows.append({
                "rank": getattr(snap, "screener_rank", 0),
                "symbol": snap.symbol,
                "screener_score": snap.screener_score,
                "close": snap.close,
                "rsi": snap.rsi,
                "rsi_crossing_50": getattr(snap, "rsi_crossing_above_50", False),
                "rsi_turning_up": getattr(snap, "rsi_turning_up", False),
                "pullback_pct": snap.pullback_pct,
                "pullback_days": snap.pullback_days,
                "adx": snap.adx,
                "at_ema20": snap.at_ema20_support,
                "at_ema50": snap.at_ema50_support,
                "entry_candle": snap.entry_candle,
                "stoch_cross": getattr(snap, "stoch_crossed_bullish_recently", False),
                "macd_hist_rising": getattr(snap, "macd_histogram_rising", False),
                "volume_declining_pullback": snap.volume_declining_pullback,
                "volume_confirmed": getattr(snap, "volume_confirmed", False),
                "price_breaking_up": getattr(snap, "price_breaking_up", False),
                "setup_qualified": getattr(snap, "setup_qualified", False),
                "rr_ratio": snap.rr_ratio,
                "stop_loss": snap.stop_loss_price,
                "target1": snap.target1_price,
                "target2": snap.target2_price,
                "target3": snap.target3_price,
                "supertrend": "BULL" if snap.supertrend_bullish else "BEAR",
            })
        return json.dumps(rows, indent=2)

    def _tool_stock_detail(self, symbol: str) -> str:
        snap = next((s for s in self.candidates if s.symbol == symbol), None)
        if not snap:
            available = [s.symbol for s in self.candidates]
            return json.dumps({"error": f"Symbol '{symbol}' not found in candidates", "available": available})
        return json.dumps(snap.to_dict(), indent=2)

    def analyse(self, candidates: list) -> list:
        """
        Run Claude analyst loop on screener candidates.
        
        Args:
            candidates: list of IndicatorSnapshot from screener
            
        Returns:
            list of WatchlistStock objects (0-5 items)
        """
        if not candidates:
            logger.info("Analyst Agent: No candidates found to parse.")
            return []

        self.candidates = candidates
        self.watchlist = []

        user_msg = (
            f"I have {len(candidates)} screener candidates for you to review today.\n"
            f"Market is open. Please analyse them and create the watchlist.\n"
            f"Top screener score today: {candidates[0].screener_score:.0f}/100 "
            f"on {candidates[0].symbol}."
        )

        messages = [{"role": "user", "content": user_msg}]
        logger.info(f"Analyst Agent starting sequence across {len(candidates)} tokens.")

        for iteration in range(15):
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4000,
                system=ANALYST_SYSTEM_PROMPT,
                tools=ANALYST_TOOLS,
                messages=messages
            )

            logger.debug(f"Analyst iteration {iteration+1}: {response.stop_reason}")
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info(f"Analyst Tool Invocation: {block.name}")
                        result = self._execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})

            if self.watchlist is not None and "submit_watchlist" in str(
                [b.name for b in response.content if hasattr(b, "name")]
            ):
                break

        # Convert raw tool dictionary returns to clean WatchlistStock objects
        result = []
        for item in self.watchlist:
            snap = next((s for s in candidates if s.symbol == item.get("symbol")), None)
            ws = WatchlistStock(
                symbol=item.get("symbol", ""),
                conviction_score=item.get("conviction_score", 0),
                entry_zone_low=item.get("entry_zone_low", 0),
                entry_zone_high=item.get("entry_zone_high", 0),
                stop_loss=item.get("stop_loss", 0),
                target1=item.get("target1", 0),
                target2=item.get("target2", 0),
                target3=item.get("target3", 0),
                thesis=item.get("thesis", ""),
                risk_factors=item.get("risk_factors", ""),
                sector=item.get("sector", ""),
                hold_days_estimate=item.get("hold_days_estimate", 20),
                setup_type=item.get("setup_type", "pullback"),
                screener_score=snap.screener_score if snap else 0,
                screener_rank=snap.screener_rank if snap else 0,
                current_price=snap.close if snap else 0,
            )
            result.append(ws)

        # Sort watch matrix by strict descending conviction priority
        result.sort(key=lambda x: x.conviction_score, reverse=True)

        logger.info(
            f"Analyst complete: {len(result)} stocks selected\n" +
            "\n".join(
                f" {ws.symbol}: conviction={ws.conviction_score} | "
                f"entry ₹{ws.entry_zone_low}-₹{ws.entry_zone_high} | "
                f"stop ₹{ws.stop_loss} | {ws.thesis[:60]}..."
                for ws in result
            )
        )

        return result


# Singleton Engine Instance Mapping
analyst_agent = AnalystAgent()