import os
from dataclasses import dataclass, field, asdict
from typing import Optional
import numpy as np
import pandas as pd
from loguru import logger

try:
    import pandas_ta as ta
except ImportError:
    logger.error("pandas-ta module is not installed. Execute: pip install pandas-ta")

from config.settings import indicator_config as C


@dataclass
class IndicatorSnapshot:
    """Computed technical data structure representing an isolated asset's Daily State."""
    symbol: str

    # Fundamental Price Properties
    close: float = 0.0
    open_: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: int = 0

    # Moving Average Ensembles
    ema20: float = 0.0
    ema50: float = 0.0
    ema200: float = 0.0
    price_above_ema20: bool = False
    price_above_ema50: bool = False
    price_above_ema200: bool = False
    ema20_above_ema50: bool = False
    ema50_slope_rising: bool = False   

    # Directional Strength Matrix
    adx: float = 0.0
    adx_strong: bool = False           

    # Moving Average Convergence Divergence Vectors
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    macd_bullish: bool = False
    macd_histogram_rising: bool = False  
    macd_crossed_bullish_recently: bool = False

    # Relative Strength Indices
    rsi: float = 0.0
    rsi_in_pullback_zone: bool = False  
    rsi_crossing_above_50: bool = False  
    rsi_in_buy_zone: bool = False      
    rsi_overbought: bool = False        

    # Stochastic Trackers
    stoch_k: float = 0.0
    stoch_p: float = 0.0
    stoch_bullish: bool = False
    stoch_crossed_bullish_recently: bool = False  

    # Volatility Risk Layouts (Turtle Trader Anchor Matrix)
    atr: float = 0.0
    stop_loss_price: float = 0.0       
    target1_price: float = 0.0         
    target2_price: float = 0.0         
    target3_price: float = 0.0         
    rr_ratio: float = 0.0

    # Bollinger Bands Runway Limits
    bb_upper: float = 0.0
    bb_mid: float = 0.0
    bb_lower: float = 0.0
    price_near_lower_band: bool = False
    price_near_upper_band: bool = False

    # Supertrend Structural Boundaries
    supertrend_value: float = 0.0
    supertrend_bullish: bool = False

    # Volumetric Validation Flags
    volume_ma20: float = 0.0
    volume_ratio: float = 0.0
    volume_confirmed: bool = False     
    volume_declining_pullback: bool = False  

    # Statistical Pullback State Tracking
    recent_high: float = 0.0           
    pullback_pct: float = 0.0          
    pullback_days: int = 0             
    is_healthy_pullback: bool = False   
    at_ema20_support: bool = False     
    at_ema50_support: bool = False     
    entry_candle: bool = False         

    # Quantitative Rank Sorting Metrics
    screener_score: float = 0.0
    screener_rank: int = 0             

    # Institutional Reversal Confirmation Elements
    rsi_turning_up: bool = False
    price_breaking_up: bool = False
    setup_qualified: bool = False      

    # Strategy Evaluation Vectors
    entry_score: int = 0
    entry_conditions: dict = field(default_factory=dict)
    summary: str = ""
    
    # NEW: Fundamental Data Injection Payload
    fundamentals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class IndicatorEngine:
    """Computes high-availability indicator arrays for multi-asset daily dataframes."""

    def compute(self, symbol: str, df: pd.DataFrame) -> Optional[IndicatorSnapshot]:
        if len(df) < C.MIN_HISTORY_DAYS:
            logger.warning(f"{symbol}: Insufficient historical data pool length ({len(df)} bars found).")
            return None

        df = df.copy().reset_index(drop=True)
        snap = IndicatorSnapshot(symbol=symbol)

        try:
            latest = df.iloc[-1]
            snap.close = round(float(latest["close"]), 2)
            snap.open_ = round(float(latest["open"]), 2)
            snap.high = round(float(latest["high"]), 2)
            snap.low = round(float(latest["low"]), 2)
            snap.volume = int(latest["volume"])

            # 1. Moving Averages Architecture
            df["ema20"] = ta.ema(df["close"], length=int(C.EMA_FAST))
            df["ema50"] = ta.ema(df["close"], length=int(C.EMA_SLOW))
            df["ema200"] = ta.ema(df["close"], length=int(C.EMA_TREND))

            snap.ema20 = round(float(df["ema20"].iloc[-1]), 2)
            snap.ema50 = round(float(df["ema50"].iloc[-1]), 2)
            snap.ema200 = round(float(df["ema200"].iloc[-1]), 2)

            snap.price_above_ema20 = snap.close > snap.ema20
            snap.price_above_ema50 = snap.close > snap.ema50
            snap.price_above_ema200 = snap.close > snap.ema200
            snap.ema20_above_ema50 = snap.ema20 > snap.ema50

            # Historical baseline slope check
            ema50_vals = df["ema50"].iloc[-5:]
            if len(ema50_vals) >= 5:
                older = float(df["ema50"].iloc[-5])
                snap.ema50_slope_rising = snap.ema50 > older

            # 2. Average Directional Index (ADX)
            adx_df = ta.adx(df["high"], df["low"], df["close"], length=int(C.ADX_PERIOD))
            if adx_df is not None and not adx_df.empty:
                adx_col = f"ADX_{C.ADX_PERIOD}"
                if adx_col in adx_df.columns:
                    snap.adx = round(float(adx_df[adx_col].iloc[-1]), 2)
                    snap.adx_strong = snap.adx >= C.ADX_MIN

            # 3. MACD Momentum Tracking
            macd_df = ta.macd(df["close"], fast=int(C.MACD_FAST), slow=int(C.MACD_SLOW), signal=int(C.MACD_SIGNAL))
            if macd_df is not None and not macd_df.empty:
                mc = f"MACD_{C.MACD_FAST}_{C.MACD_SLOW}_{C.MACD_SIGNAL}"
                ms = f"MACDs_{C.MACD_FAST}_{C.MACD_SLOW}_{C.MACD_SIGNAL}"
                mh = f"MACDh_{C.MACD_FAST}_{C.MACD_SLOW}_{C.MACD_SIGNAL}"
                
                if mc in macd_df.columns:
                    snap.macd_line = round(float(macd_df[mc].iloc[-1]), 4)
                    snap.macd_signal = round(float(macd_df[ms].iloc[-1]), 4)
                    snap.macd_histogram = round(float(macd_df[mh].iloc[-1]), 4)
                    snap.macd_bullish = snap.macd_line > snap.macd_signal
                    
                    if len(macd_df) > 1:
                        prev_hist = float(macd_df[mh].iloc[-2])
                        snap.macd_histogram_rising = snap.macd_histogram > prev_hist

                    for i in range(1, min(4, len(macd_df))):
                        if macd_df[mc].iloc[-i] > macd_df[ms].iloc[-i] and macd_df[mc].iloc[-i-1] <= macd_df[ms].iloc[-i-1]:
                            snap.macd_crossed_bullish_recently = True
                            break

            # 4. Relative Strength Index (RSI)
            df["rsi"] = ta.rsi(df["close"], length=int(C.RSI_PERIOD))
            snap.rsi = round(float(df["rsi"].iloc[-1]), 2)
            snap.rsi_in_pullback_zone = C.RSI_PULLBACK_MIN <= snap.rsi <= C.RSI_PULLBACK_MAX
            snap.rsi_in_buy_zone = C.RSI_BUY_MIN <= snap.rsi <= C.RSI_BUY_MAX
            snap.rsi_overbought = snap.rsi > C.RSI_OVERBOUGHT

            if len(df) > 1 and df["rsi"].dropna().shape[0] >= 2:
                prev_rsi = float(df["rsi"].dropna().iloc[-2])
                snap.rsi_crossing_above_50 = (prev_rsi <= 50) and (snap.rsi >= 50)

            # 5. Stochastic
            stoch_df = ta.stoch(df["high"], df["low"], df["close"], length=int(C.STOCH_K), d=int(C.STOCH_D), smooth_k=int(C.STOCH_SMOOTH))
            if stoch_df is not None and not stoch_df.empty:
                k_col = f"STOCHk_{C.STOCH_K}_{C.STOCH_D}_{C.STOCH_SMOOTH}"
                d_col = f"STOCHd_{C.STOCH_K}_{C.STOCH_D}_{C.STOCH_SMOOTH}"
                if k_col in stoch_df.columns and d_col in stoch_df.columns:
                    snap.stoch_k = round(float(stoch_df[k_col].iloc[-1]), 2)
                    snap.stoch_p = round(float(stoch_df[d_col].iloc[-1]), 2)
                    snap.stoch_bullish = snap.stoch_k > snap.stoch_p
                    if len(stoch_df) > 1:
                        prev_k = float(stoch_df[k_col].iloc[-2])
                        prev_d = float(stoch_df[d_col].iloc[-2])
                        snap.stoch_crossed_bullish_recently = (prev_k <= prev_d) and (snap.stoch_k > snap.stoch_p)

            # 6. Average True Range (ATR)
            df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=int(C.ATR_PERIOD))
            snap.atr = round(float(df["atr"].iloc[-1]), 2)
            
            snap.stop_loss_price = round(snap.close - (C.ATR_STOP_MULT * snap.atr), 2)
            snap.target1_price = round(snap.close + (C.ATR_TARGET1_MULT * snap.atr), 2)
            snap.target2_price = round(snap.close + (C.ATR_TARGET2_MULT * snap.atr), 2)
            snap.target3_price = round(snap.close + (C.ATR_TARGET3_MULT * snap.atr), 2)
            
            stop_dist = max(0.01, snap.close - snap.stop_loss_price)
            t2_dist = snap.target2_price - snap.close
            snap.rr_ratio = round(t2_dist / stop_dist, 2)

            # 7. Bollinger Bands
            bb_df = ta.bbands(df["close"], length=int(C.BB_PERIOD), std=float(C.BB_STD))
            if bb_df is not None and not bb_df.empty:
                u_col = f"BBU_{C.BB_PERIOD}_{float(C.BB_STD)}"
                m_col = f"BBM_{C.BB_PERIOD}_{float(C.BB_STD)}"
                l_col = f"BBL_{C.BB_PERIOD}_{float(C.BB_STD)}"
                if u_col in bb_df.columns:
                    snap.bb_upper = round(float(bb_df[u_col].iloc[-1]), 2)
                    snap.bb_mid = round(float(bb_df[m_col].iloc[-1]), 2)
                    snap.bb_lower = round(float(bb_df[l_col].iloc[-1]), 2)
                    snap.price_near_lower_band = snap.close <= snap.bb_lower * 1.015
                    snap.price_near_upper_band = snap.close >= snap.bb_upper * 0.985

            # 8. Supertrend
            st_df = ta.supertrend(df["high"], df["low"], df["close"], length=int(C.SUPERTREND_PERIOD), multiplier=float(C.SUPERTREND_MULT))
            if st_df is not None and not st_df.empty:
                st_val_col = f"SUPERT_{C.SUPERTREND_PERIOD}_{float(C.SUPERTREND_MULT)}"
                st_dir_col = f"SUPERTd_{C.SUPERTREND_PERIOD}_{float(C.SUPERTREND_MULT)}"
                if st_val_col in st_df.columns:
                    snap.supertrend_value = round(float(st_df[st_val_col].iloc[-1]), 2)
                    snap.supertrend_bullish = st_df[st_dir_col].iloc[-1] == 1

            # 9. Volume Moving Average Verification
            df["vol_ma20"] = df["volume"].rolling(int(C.VOLUME_MA_PERIOD)).mean()
            snap.volume_ma20 = round(float(df["vol_ma20"].iloc[-1]), 0)
            snap.volume_ratio = round(snap.volume / snap.volume_ma20, 2) if snap.volume_ma20 > 0 else 0.0
            
            snap.volume_confirmed = snap.volume_ratio >= C.VOLUME_RATIO_MIN

            if len(df) >= 5:
                recent_vols = df["volume"].iloc[-5:-1].values
                snap.volume_declining_pullback = bool(recent_vols[-1] < recent_vols[0])

            # 10. Macro Pullback Analytics
            lookback = min(20, len(df) - 1)
            recent_closes = df["close"].iloc[-lookback:]
            snap.recent_high = round(float(recent_closes.max()), 2)
            if snap.recent_high > 0:
                snap.pullback_pct = round((snap.recent_high - snap.close) / snap.recent_high * 100, 2)
            
            high_idx = recent_closes.values.argmax()
            snap.pullback_days = int(lookback - high_idx - 1)

            snap.is_healthy_pullback = (
                C.PULLBACK_MIN_PCT <= snap.pullback_pct <= C.PULLBACK_MAX_PCT
            ) and (2 <= snap.pullback_days <= C.PULLBACK_MAX_DAYS)

            ema20_gap_pct = abs(snap.close - snap.ema20) / snap.ema20 * 100 if snap.ema20 > 0 else 999
            ema50_gap_pct = abs(snap.close - snap.ema50) / snap.ema50 * 100 if snap.ema50 > 0 else 999
            snap.at_ema20_support = ema20_gap_pct <= 1.5 and snap.close >= snap.ema20 * 0.98
            snap.at_ema50_support = ema50_gap_pct <= 2.0 and snap.close >= snap.ema50 * 0.97

            if len(df) >= 2:
                prev_high = float(df["high"].iloc[-2])
                snap.entry_candle = snap.close > prev_high and snap.close > snap.open_

            # --- THE GOLDEN REVERSAL CONFIRMATION GATES ---
            if len(df) >= 2:
                snap.rsi_turning_up = float(df["rsi"].iloc[-1]) > float(df["rsi"].iloc[-2])
                snap.price_breaking_up = snap.close > float(df["high"].iloc[-2])
            else:
                snap.rsi_turning_up = False
                snap.price_breaking_up = False

            # 11. Quant Model Scoring Mappings
            snap.screener_score = self._compute_screener_score(snap)

            snap.setup_qualified = (
                snap.price_above_ema200 and 
                snap.rsi_in_pullback_zone and 
                snap.rsi_turning_up and 
                snap.price_breaking_up and 
                snap.volume_confirmed
            )

            snap.entry_conditions = {
                "trend_aligned": snap.price_above_ema200 and snap.ema50_slope_rising,
                "healthy_pullback": snap.is_healthy_pullback,
                "rsi_pullback": snap.rsi_in_pullback_zone or snap.rsi_crossing_above_50,
                "resumption": snap.entry_candle or snap.stoch_crossed_bullish_recently,
                "volume_ok": snap.volume_confirmed or snap.volume_declining_pullback,
            }
            
            snap.entry_score = sum(snap.entry_conditions.values())
            snap.summary = self._build_summary(snap)

            return snap

        except Exception as e:
            logger.error(f"Critical execution error computing indicator arrays for {symbol}: {e}")
            return None

    def _compute_screener_score(self, s: IndicatorSnapshot) -> float:
        score = 0.0
        if s.price_above_ema200: score += 15
        if s.ema50_slope_rising: score += 10
        if s.price_above_ema50: score += 8
        if s.adx_strong: score += 7
        if s.is_healthy_pullback: score += 20
        if s.at_ema20_support: score += 8
        if s.at_ema50_support: score += 7
        if s.rsi_crossing_above_50: score += 10
        if s.entry_candle: score += 8
        if s.stoch_crossed_bullish_recently: score += 4
        if s.macd_histogram_rising: score += 3
        return round(score, 1)

    def _build_summary(self, s: IndicatorSnapshot) -> str:
        return (
            f"₹{s.close} | EMA20=₹{s.ema20} EMA50=₹{s.ema50} EMA200=₹{s.ema200} | "
            f"RSI {s.rsi:.1f} | ADX {s.adx:.1f} | Pullback {s.pullback_pct:.1f}% "
            f"over {s.pullback_days}d | Score {s.screener_score}/100 | "
            f"{'✓' if s.is_healthy_pullback else 'X'} pullback | "
            f"{'✓' if s.entry_candle else 'X'} entry candle"
        )

    def compute_universe(self, universe_data: dict) -> dict:
        results = {}
        for symbol, df in universe_data.items():
            snap = self.compute(symbol, df)
            if snap:
                results[symbol] = snap
        return results

# Singleton Instantiation Reference
indicator_engine = IndicatorEngine()