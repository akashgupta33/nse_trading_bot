import json
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Any, Dict
from loguru import logger

import anthropic
from config.settings import settings
import json
import numpy as np

class NumpyEncoder(json.JSONEncoder):
    """Translates NumPy data types into standard Python types for JSON serialization."""
    def default(self, obj):
        if isinstance(obj, (np.bool_, bool)):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)

@dataclass
class WatchlistStock:
    """A stock selected by the Analyst Agent for the Execution Agent."""
    symbol: str
    conviction_score: float = 0.0     
    entry_zone_low: float = 0.0      
    entry_zone_high: float = 0.0
    stop_loss: float = 0.0           
    target1: float = 0.0             
    target2: float = 0.0             
    target3: float = 0.0             
    thesis: str = ""                 
    risk_factors: str = ""           
    sector: str = ""
    hold_days_estimate: int = 20     
    setup_type: str = ""             
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
            "screener score, RSI, pullback %, volume, AND fundamental P/E and Sector data. "
            "Call this first to see the full list."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_stock_detail",
        "description": (
            "Get the complete technical and fundamental snapshot for a specific stock. "
            "Includes all indicator values, support levels, and corporate valuations."
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
            "Only include stocks you genuinely believe have strong technical and fundamental setups."
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
                            "entry_zone_high", "stop_loss", "target1", "target2", "target3", "thesis"
                        ],
                    },
                    "minItems": 0,
                    "maxItems": 4,
                    "description": "Selected stocks. Submit empty list if nothing meets the strict dual-mandate bar.",
                }
            },
            "required": ["watchlist"],
        },
    },
]

ANALYST_SYSTEM_PROMPT = """You are a senior equity analyst at a top Indian fund house.
Your job: review screener candidates and identify 1-4 stocks with the strongest
technical setups BACKED BY fundamental health for swing trading (holding 2-6 weeks).

DUAL-MANDATE STRATEGY YOU ARE LOOKING FOR:
1. Technical Base: Stock pulled back, RSI hooked up (40-57), breaking out on 1.1x volume.
2. Fundamental Filter: Review the P/E ratio, Sector, and Revenue Growth. 
   - High P/E is acceptable ONLY if it's a high-growth sector.
   - If fundamental data is missing (N/A), rely entirely on technical volume accumulation.

CONVICTION SCORING GUIDE (1-10):
9-10: Perfect technical breakout AND strong fundamental valuation/growth.
7-8: Great technicals, acceptable or average fundamentals.
5-6: Good technicals, but poor fundamentals (High risk, avoid unless volume is insane).
Below 5: Do NOT include under any circumstances.

PROCESS:
1. Call get_candidate_list() to see all candidates ranked by screener score.
2. Deep-dive into top candidates using get_stock_detail() to view fundamentals.
3. Skip any stock failing the strict confirmation rules.
4. Call submit_watchlist() with chosen stocks. Empty is completely acceptable if nothing qualifies."""


class AnalystAgent:
    """Evaluates technically screened stocks against fundamental valuation data."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.candidates: List[Any] = []
        self.watchlist: List[Dict[str, Any]] = []

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
            # Safely extract injected fundamental data
            fund = getattr(snap, "fundamentals", {})
            sector = fund.get("sector", "N/A")
            pe = fund.get("trailingPE", "N/A")
            
            rows.append({
                "rank": getattr(snap, "screener_rank", 0),
                "symbol": snap.symbol,
                "screener_score": snap.screener_score,
                "close": snap.close,
                "sector": sector,
                "trailing_PE": pe,
                "rsi": snap.rsi,
                "rsi_turning_up": getattr(snap, "rsi_turning_up", False),
                "volume_confirmed": getattr(snap, "volume_confirmed", False),
                "setup_qualified": getattr(snap, "setup_qualified", False),
                "rr_ratio": snap.rr_ratio,
            })
        return json.dumps(rows, indent=2)

    def _tool_stock_detail(self, symbol: str) -> str:
        snap = next((s for s in self.candidates if s.symbol == symbol), None)
        if not snap:
            available = [s.symbol for s in self.candidates]
            return json.dumps({"error": f"Symbol '{symbol}' not found in candidates", "available": available})
        
        # Combine Technicals and Fundamentals into one clean JSON block
        data = snap.to_dict()
        data["fundamentals"] = getattr(snap, "fundamentals", {})
        return json.dumps(data, indent=2, cls=NumpyEncoder)

    def analyse(self, candidates: list) -> list:
        """
        Run Claude analyst loop on screener candidates with fundamental injection.
        """
        if not candidates:
            logger.info("Analyst Agent: No candidates found to parse.")
            return []

        self.candidates = candidates
        self.watchlist = []

        user_msg = (
            f"I have {len(candidates)} mathematically qualified candidates for you to review today.\n"
            f"Please analyse their technicals and fundamentals to create the optimal watchlist.\n"
            f"Top screener score today: {candidates[0].screener_score:.0f}/100 "
            f"on {candidates[0].symbol}."
        )

        messages = [{"role": "user", "content": user_msg}]
        logger.info(f"Analyst Agent starting sequence across {len(candidates)} tokens.")

        for iteration in range(12):
            response = self.client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=4000,
                system=ANALYST_SYSTEM_PROMPT,
                tools=ANALYST_TOOLS,
                messages=messages
            )

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

            if self.watchlist and "submit_watchlist" in str([getattr(b, "name", "") for b in response.content]):
                break

        # Convert raw tool dictionary returns to clean WatchlistStock objects
        result = []
        for item in self.watchlist:
            snap = next((s for s in candidates if s.symbol == item.get("symbol")), None)
            fund = getattr(snap, "fundamentals", {}) if snap else {}
            
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
                sector=item.get("sector", fund.get("sector", "Unknown")),
                hold_days_estimate=item.get("hold_days_estimate", 21),
                setup_type=item.get("setup_type", "institutional_pullback"),
                screener_score=snap.screener_score if snap else 0,
                screener_rank=snap.screener_rank if snap else 0,
                current_price=snap.close if snap else 0,
            )
            result.append(ws)

        # Sort watch matrix by strict descending conviction priority
        result.sort(key=lambda x: x.conviction_score, reverse=True)

        if result:
            logger.success(
                f"Analyst complete: {len(result)} stocks selected\n" +
                "\n".join(
                    f"  {ws.symbol}: Conviction={ws.conviction_score}/10 | "
                    f"Target 2: ₹{ws.target2} | Stop: ₹{ws.stop_loss} | {ws.sector}"
                    for ws in result
                )
            )
        else:
            logger.warning("Analyst complete: 0 stocks met the Dual-Mandate standard today.")

        return result


# Singleton Engine Instance Mapping
analyst_agent = AnalystAgent()