import time
from datetime import datetime
from typing import Optional
import pandas as pd
import pytz
from loguru import logger

from config.settings import settings, indicator_config as C
from data.fyers_client import fyers_client
from indicators.engine import indicator_engine, IndicatorSnapshot

IST = pytz.timezone("Asia/Kolkata")

class ScreenerAgent:
    """
    Screener Agent - Layer 1 of the 3-agent system.
    Scans the full NSE watchlist securely forcing Daily ("D") resolution.
    Outputs the top N stocks ranked by screener_score for the Analyst Agent.
    """

    def run(self) -> list:
        """
        Full screener pipeline.
        Returns list of top-N IndicatorSnapshot objects, ranked best first.
        Returns empty list if market is in bear mode (Nifty below EMA200).
        """
        logger.info(f"=== SCREENER START - scanning {len(settings.nse_watchlist)} symbols ===")

        # ----- Step 1: Check Nifty macro filter -----
        if settings.nifty_above_ema200_required:
            nifty_ok = self._check_nifty_trend()
            if not nifty_ok:
                logger.warning("Nifty50 is below EMA200 - systemic bear market filter active. Halting new longs.")
                return [] 

        # ----- Step 2: Fetch clean Daily ("D") data for all watchlist symbols -----
        universe_data = {}
        for sym in settings.nse_watchlist:
            if "NIFTY50" in sym:
                continue
            try:
                # fyers_client already returns clean, tz-naive data. No further conversion needed.
                df = fyers_client.get_historical(sym, days=220, resolution="D")
                if df is not None and not df.empty:
                    universe_data[sym] = df
                
                # API rate limit protection
                time.sleep(0.12)
            except Exception as e:
                logger.error(f"Screener data extraction failed for {sym}: {e}")
                continue

        if not universe_data:
            logger.error("No daily arrays fetched from broker - screener aborted.")
            return []

        # ----- Step 3: Compute technical snapshots -----
        snapshots = indicator_engine.compute_universe(universe_data)
        logger.info(f"Computed mathematical matrices for {len(snapshots)} symbols.")

        # ----- Step 4: Apply strict institutional confirmation gate -----
        candidates = []
        for symbol, snap in snapshots.items():
            # Only accept assets that passed ALL directional, volume, and structural checks
            if getattr(snap, "setup_qualified", False):
                candidates.append(snap)

        logger.info(f"Candidates surviving the Golden Gate: {len(candidates)}")

        # ----- Step 5: Rank by screener_score -----
        candidates.sort(key=lambda s: s.screener_score, reverse=True)

        for i, snap in enumerate(candidates):
            snap.screener_rank = i + 1

        top_n = candidates[:settings.screener_top_n]

        if top_n:
            logger.success(
                f"Top {len(top_n)} Qualified Targets:\n" +
                "\n".join(
                    f"  #{s.screener_rank} {s.symbol}: Score={s.screener_score}/100 | "
                    f"₹{s.close} | RSI={s.rsi:.1f} | Pullback={s.pullback_pct:.1f}%"
                    for s in top_n[:10]  
                )
            )

        return top_n

    def _check_nifty_trend(self) -> bool:
        """Returns True if Nifty50 is above its 200 EMA (healthy market environment)."""
        try:
            df = fyers_client.get_historical(settings.nifty_symbol, days=220, resolution="D")
            if df is None or df.empty:
                logger.warning("Could not fetch Nifty data - assuming market ok to prevent hard block.")
                return True
                
            # Compute the indicator snapshot for Nifty50
            snap = indicator_engine.compute(settings.nifty_symbol, df)
            
            if not snap:
                return True

            is_above = snap.price_above_ema200
            logger.info(
                f"NIFTY 50 MACRO TREND: ₹{snap.close} | 200 EMA: ₹{snap.ema200} | "
                f"{'BULLISH CONFIRMED ✓' if is_above else 'BEARISH X'}"
            )
            return is_above

        except Exception as e:
            logger.error(f"Nifty macro check exception: {e}")
            return True


# Singleton
screener_agent = ScreenerAgent()