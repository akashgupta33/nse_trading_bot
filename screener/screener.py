from datetime import datetime
from typing import Optional
import pytz
from loguru import logger

from config.settings import settings, indicator_config as C, MarketTime
from data.fyers_client import fyers_client
from indicators.engine import indicator_engine, IndicatorSnapshot

IST = pytz.timezone("Asia/Kolkata")

class ScreenerAgent:
    """
    Screener Agent - Layer 1 of the 3-agent system.

    Scans the full NSE watchlist (60+ stocks) using pure technical indicators.
    No Claude involved - pure Python, fast, cheap.
    Outputs the top N stocks ranked by screener_score for the Analyst Agent.

    Filters:
      - Price above EMA200 (uptrend)
      - ADX >= 20 (trending)
      - Nifty50 itself above EMA200 (market not in bear mode)
      - Price >= min_price
      - Healthy pullback exists (3-15% from recent high)

    Ranks by screener_score (0-100) computed in indicator engine.
    """

    def run(self) -> Optional[list]:
        """
        Full screener pipeline.
        Returns list of top-N IndicatorSnapshot objects, ranked best first.
        Returns None if market is in bear mode (Nifty below EMA200).
        """
        logger.info(f"=== SCREENER START - scanning {len(settings.nse_watchlist)} symbols ===")

        # ----- Step 1: Check Nifty macro filter -----
        if settings.nifty_above_ema200_required:
            nifty_ok = self._check_nifty_trend()
            if not nifty_ok:
                logger.warning("Nifty50 is below EMA200 - bear market filter active. No new longs.")
                return []  # Empty list = screener ran but found nothing safe

        # ----- Step 2: Fetch data for all watchlist symbols -----
        watchlist = [s for s in settings.nse_watchlist if "NIFTY50" not in s]
        universe_data = fyers_client.fetch_universe_data(watchlist, days=250)

        if not universe_data:
            logger.error("No data fetched from Fyers - screener aborted")
            return None

        # ----- Step 3: Compute indicators -----
        snapshots = indicator_engine.compute_universe(universe_data)
        logger.info(f"Computed indicators for {len(snapshots)} symbols")

        # ----- Step 4: Apply hard filters -----
        candidates = []
        filtered_out = {"no_ema200": 0, "no_adx": 0, "no_pullback": 0, "price_too_low": 0, "low_score": 0}

        for symbol, snap in snapshots.items():
            # Hard filter 1: must be in uptrend
            if not snap.price_above_ema200:
                filtered_out["no_ema200"] += 1
                continue

            # Hard filter 2: must have trending ADX
            if not snap.adx_strong:
                filtered_out["no_adx"] += 1
                continue

            # Hard filter 3: price must be above minimum
            if snap.close < settings.screener_min_price:
                filtered_out["price_too_low"] += 1
                continue

            # Hard filter 4: must have a pullback (avoid stocks at all-time highs)
            if snap.pullback_pct < C.PULLBACK_MIN_PCT:
                filtered_out["no_pullback"] += 1
                continue

            # Soft filter: minimum screener score
            if snap.screener_score < 20:
                filtered_out["low_score"] += 1
                continue

            candidates.append(snap)

        logger.info(
            f"Filtered out: EMA200={filtered_out['no_ema200']} | "
            f"ADX={filtered_out['no_adx']} | pullback={filtered_out['no_pullback']} | "
            f"low_score={filtered_out['low_score']} | price={filtered_out['price_too_low']}"
        )
        logger.info(f"Candidates after filter: {len(candidates)}")

        # ----- Step 5: Rank by screener_score -----
        candidates.sort(key=lambda s: s.screener_score, reverse=True)

        # Assign ranks
        for i, snap in enumerate(candidates):
            snap.screener_rank = i + 1

        # Return top N
        top_n = candidates[:settings.screener_top_n]

        logger.info(
            f"Top {len(top_n)} candidates:\n" +
            "\n".join(
                f"  #{s.screener_rank} {s.symbol}: score={s.screener_score} | "
                f"₹{s.close} | RSI={s.rsi} | pullback={s.pullback_pct:.1f}%"
                for s in top_n[:10]  # log first 10
            )
        )

        return top_n

    def _check_nifty_trend(self) -> bool:
        """Returns True if Nifty50 is above its 200 EMA (healthy market)."""
        try:
            nifty_data = fyers_client.fetch_universe_data(
                [settings.nifty_symbol], days=250
            )
            if not nifty_data:
                logger.warning("Could not fetch Nifty data - assuming market ok")
                return True

            nifty_snaps = indicator_engine.compute_universe(nifty_data)
            nifty_snap = next(iter(nifty_snaps.values()), None)
            if not nifty_snap:
                return True

            is_above = nifty_snap.price_above_ema200
            logger.info(
                f"Nifty50: ₹{nifty_snap.close} | EMA200: ₹{nifty_snap.ema200} | "
                f"{'ABOVE ✓' if is_above else 'BELOW X'}"
            )
            return is_above

        except Exception as e:
            logger.error(f"Nifty check error: {e}")
            return True  # fail open - don't block trading on data errors


# Singleton
screener_agent = ScreenerAgent()