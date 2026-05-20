import os
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List

class Settings(BaseSettings):
    # ----- Fyers Core Credentials -----
    fyers_client_id: str = Field(..., env="FYERS_CLIENT_ID")
    fyers_secret_key: str = Field(..., env="FYERS_SECRET_KEY")
    fyers_redirect_uri: str = Field("https://127.0.0.1:8080/", env="FYERS_REDIRECT_URI")
    fyers_access_token: str = Field(..., env="FYERS_ACCESS_TOKEN")

    # ----- Anthropic API Vector -----
    anthropic_api_key: str = Field(..., env="ANTHROPIC_API_KEY")

    # ----- Telegram Notification Channel -----
    telegram_bot_token: str = Field(..., env="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(..., env="TELEGRAM_CHAT_ID")

    # ----- System Trading Mode -----
    trading_mode: str = Field("paper", env="TRADING_MODE")

    # ----- Institutional Capital Risk Allocations -----
    paper_capital: float = Field(1000000.0, env="PAPER_CAPITAL")    # ₹10 Lakh Simulation Base
    live_capital: float = Field(20000.0, env="LIVE_CAPITAL")         # ₹20,000 Real Capital Anchor
    risk_per_trade_pct: float = Field(1.5, env="RISK_PER_TRADE_PCT") # Strict 1.5% Volatility Risk Parity Limit
    max_positions: int = Field(4, env="MAX_POSITIONS")               # Synchronized to 4-slot position matrix
    cash_reserve_pct: float = Field(10.0, env="CASH_RESERVE_PCT")    # Optimized 10% liquidity buffer

    # ----- Portfolio Strategy Time Parameters -----
    min_conviction_score: float = Field(7.0, env="MIN_CONVICTION_SCORE")
    max_hold_days: int = Field(45, env="MAX_HOLD_DAYS")                  
    stale_trade_days: int = Field(15, env="STALE_TRADE_DAYS")            
    stale_trade_min_gain_pct: float = Field(3.0, env="STALE_TRADE_MIN_GAIN_PCT")

    # ----- Curated High-Volume Nifty-50 Screener Universe -----
    screener_top_n: int = Field(20, env="SCREENER_TOP_N")                
    screener_min_market_cap_cr: float = Field(500, env="SCREENER_MIN_MARKET_CAP_CR")
    screener_min_price: float = Field(50.0, env="SCREENER_MIN_PRICE")
    
    nse_watchlist_raw: str = Field(
        "NSE:RELIANCE-EQ,NSE:TCS-EQ,NSE:HDFCBANK-EQ,NSE:ICICIBANK-EQ,NSE:INFY-EQ,"
        "NSE:ITC-EQ,NSE:SBIN-EQ,NSE:BHARTIARTL-EQ,NSE:BAJFINANCE-EQ,NSE:LT-EQ,"
        "NSE:MARUTI-EQ,NSE:SUNPHARMA-EQ,NSE:WIPRO-EQ,NSE:ULTRACEMCO-EQ,NSE:HCLTECH-EQ,"
        "NSE:POWERGRID-EQ,NSE:NTPC-EQ,NSE:ONGC-EQ,NSE:COALINDIA-EQ,NSE:M&M-EQ,"
        "NSE:TATAMOTORS-EQ,NSE:TATACONSUM-EQ,NSE:BAJAJFINSV-EQ,NSE:BEL-EQ,NSE:HINDALCO-EQ,"
        "NSE:JSWSTEEL-EQ,NSE:TATASTEEL-EQ,NSE:HAVELLS-EQ,NSE:PIDILITIND-EQ,NSE:INDIGO-EQ,"
        "NSE:ZOMATO-EQ,NSE:HAL-EQ,NSE:BHEL-EQ,NSE:IRCTC-EQ,NSE:COFORGE-EQ,"
        "NSE:ASTRAL-EQ,NSE:SUPREMEIND-EQ,NSE:CONCOR-EQ,NSE:PETRONET-EQ,NSE:SAIL-EQ,"
        "NSE:VEDL-EQ,NSE:LUPIN-EQ,NSE:DEEPAKNTR-EQ,NSE:PIIND-EQ,"
        "NSE:VOLTAS-EQ,NSE:APOLLOTYRE-EQ,NSE:EXIDEIND-EQ,NSE:DLF-EQ,NSE:PRESTIGE-EQ",
        env="NSE_WATCHLIST"
    )

    # ----- Intraday Execution Monitor Timing -----
    intraday_check_interval_min: int = Field(30, env="INTRADAY_CHECK_INTERVAL_MIN")
    intraday_claude_trigger_pct: float = Field(3.0, env="INTRADAY_CLAUDE_TRIGGER_PCT")

    # ----- Macro Index Filters -----
    nifty_symbol: str = Field("NSE:NIFTY50-INDEX", env="NIFTY_SYMBOL")
    nifty_above_ema200_required: bool = Field(True, env="NIFTY_ABOVE_EMA200_REQUIRED")

    # ----- Database Schema Destination -----
    db_path: str = Field("logs/trades.db", env="DB_PATH")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    @property
    def capital(self) -> float:
        """Returns active available capital matrix pool."""
        return self.live_capital if self.is_live else self.paper_capital

    @property
    def position_size(self) -> float:
        """Maximum deployed capital allowed per position vector slot allocation."""
        deployable = self.capital * (1 - self.cash_reserve_pct / 100)
        return deployable / self.max_positions

    @property
    def nse_watchlist(self) -> List[str]:
        return [s.strip() for s in self.nse_watchlist_raw.split(",") if s.strip()]

    @property
    def risk_amount(self) -> float:
        return self.capital * (self.risk_per_trade_pct / 100)

    @property
    def is_live(self) -> bool:
        return self.trading_mode.lower() == "live"


class IndicatorConfig:
    # --- Institutional Trend Parameters (Daily Close Bars) ---
    EMA_FAST = 20
    text_SLOW = 50
    EMA_SLOW = 50
    EMA_TREND = 200
    ADX_PERIOD = 14
    ADX_MIN = 20       

    # --- MACD Wave Structures ---
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9

    # --- Daily RSI Pullback Strategy Zones ---
    RSI_PERIOD = 14
    RSI_PULLBACK_MIN = 40.0   # Floor of professional consolidation pool
    RSI_PULLBACK_MAX = 57.0   # Optimized pullback tracking zone (Upgraded from 52.0)
    RSI_RESUMPTION = 50.0      
    RSI_OVERBOUGHT = 75.0     # Extended profit realization trigger point
    RSI_BUY_MIN = 35.0         
    RSI_BUY_MAX = 65.0         

    # --- Stochastic Settings ---
    STOCH_K = 14
    STOCH_D = 3
    STOCH_SMOOTH = 3

    # --- Volatility Buffers (Daily Timeframe ATR Scales) ---
    ATR_PERIOD = 14
    ATR_STOP_MULT = 2.5       # Positioned safely outside regular daily tracking fluctuations
    ATR_TARGET1_MULT = 2.0   
    ATR_TARGET2_MULT = 3.0    # Optimized structural swing target zone (3.0 * ATR)
    ATR_TARGET3_MULT = 5.0   

    # --- Bollinger Bands Setup ---
    BB_PERIOD = 20
    BB_STD = 2

    # --- Supertrend Settings ---
    SUPERTREND_PERIOD = 10
    SUPERTREND_MULT = 3

    # --- Volume Confirmation Benchmarks ---
    VOLUME_RATIO_MIN = 1.1   
    VOLUME_MA_PERIOD = 20
    VOLUME_DECLINING_ON_PULLBACK = True 

    # --- History Verification Boundaries ---
    MIN_HISTORY_DAYS = 220    # Clean historical baseline for 200 EMA operations

    # --- Structural Filter Limits ---
    PULLBACK_MIN_PCT = 3.0   
    PULLBACK_MAX_PCT = 15.0  
    PULLBACK_MAX_DAYS = 12   
    WEEKLY_EMA_SLOPE_LOOKBACK = 5 


class MarketTime:
    OPEN_HOUR, OPEN_MIN = 9, 15
    SCREENER_HOUR, SCREENER_MIN = 8, 45     
    ANALYST_HOUR, ANALYST_MIN = 9, 0         
    TRADE_HOUR, TRADE_MIN = 9, 20           
    INTRADAY_CHECK_START = 10, 0            
    INTRADAY_CHECK_END = 15, 0              
    EOD_REVIEW_HOUR, EOD_REVIEW_MIN = 15, 15 
    CLOSE_HOUR, CLOSE_MIN = 15, 30          


# Singleton Declarations
settings = Settings()
indicator_config = IndicatorConfig()
market_time = MarketTime()