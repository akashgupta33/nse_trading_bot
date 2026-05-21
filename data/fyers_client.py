import os
import json
import time
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import pytz
from loguru import logger

from config.settings import settings

IST = pytz.timezone("Asia/Kolkata")


class FyersClient:
    """Wraps fyers_apiv3 with auto-token management and throttled data pipelines."""

    def __init__(self):
        self.fyers = None
        self.token_path = "config/.fyers_token"

    # =============================================================================
    # # Authentication Management Layer
    # =============================================================================

    def authenticate(self) -> bool:
        """
        Full auth flow - opens browser for Fyers login.
        Call this once manually; token is cached to disk.
        """
        try:
            from fyers_apiv3 import fyersModel
            from fyers_apiv3.fyersModel import SessionModel

            session = SessionModel(
                client_id=settings.fyers_client_id,
                secret_key=settings.fyers_secret_key,
                redirect_uri=settings.fyers_redirect_uri,
                response_type="code",
                grant_type="authorization_code",
            )

            auth_url = session.generate_authcode()
            print(f"\n---> Open this URL in your browser and login:\n{auth_url}\n")
            auth_code = input("Paste the auth_code from the redirect URL: ").strip()

            session.set_token(auth_code)
            response = session.generate_token()

            if response.get("s") == "ok":
                token = response["access_token"]
                self._save_token(token)
                logger.success("Fyers authenticated successfully.")
                return True
            else:
                logger.error(f"Token generation failed: {response}")
                return False

        except Exception as e:
            logger.error(f"Fyers auth error: {e}")
            return False

    def _save_token(self, token: str):
        data = {"token": token, "date": datetime.now(IST).strftime("%Y-%m-%d")}
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        with open(self.token_path, "w") as f:
            json.dump(data, f)

    def _load_token(self) -> Optional[str]:
        if not os.path.exists(self.token_path):
            return None
        with open(self.token_path, "r") as f:
            data = json.load(f)
            
        saved_date = data.get("date", "")
        today = datetime.now(IST).strftime("%Y-%m-%d")
        
        if saved_date != today:
            logger.warning("Fyers token is from a previous day - re-auth needed.")
            return None
            
        return data.get("token")

    def connect(self) -> bool:
        """Load cached token and initialise Fyers client."""
        try:
            from fyers_apiv3 import fyersModel

            token = self._load_token()
            if not token:
                logger.error("No Fyers token. Run authenticate() first.")
                return False

            self.fyers = fyersModel.FyersModel(
                client_id=settings.fyers_client_id,
                token=token,
                log_path="logs/",
            )

            # Quick validation
            profile = self.fyers.get_profile()
            if profile.get("s") == "ok":
                logger.success("Fyers client connected.")
                return True
            else:
                logger.error(f"Fyers connection validation failed: {profile}")
                return False

        except Exception as e:
            logger.error(f"Fyers connect error: {e}")
            return False

    # =============================================================================
    # # Market Data Streams (Chunked OHLCV Ingestion)
    # =============================================================================

    def get_historical(
        self,
        symbol: str,
        days: int = 220,
        resolution: str = "D",
    ) -> Optional[pd.DataFrame]:
        """
        Fetch historical OHLCV data by chunking requests into safe 90-day windows.
        """
        if not self.fyers:
            logger.error("Fyers not connected.")
            return None

        try:
            end_date = datetime.now(IST)
            start_date = end_date - timedelta(days=days)
            
            all_candles = []
            current_start = start_date
            
            # Loop and pull in safe 90-day chunks to avoid the 'Invalid input' window trap
            while current_start < end_date:
                current_end = min(current_start + timedelta(days=90), end_date)
                
                data = {
                    "symbol": symbol,
                    "resolution": str(resolution),
                    "date_format": "1",  # "1" format for YYYY-MM-DD
                    "range_from": current_start.strftime("%Y-%m-%d"),
                    "range_to": current_end.strftime("%Y-%m-%d"),
                    "cont_flag": 1 
                }

                response = self.fyers.history(data=data)
                
                if response and response.get("s") == "ok":
                    candles = response.get("candles", [])
                    all_candles.extend(candles)
                elif response and "limit reached" in response.get("message", "").lower():
                    logger.warning(f"Fyers limit hit during loop for {symbol}. Cooling down...")
                    time.sleep(2.0)
                    continue
                
                # Move to the next 90-day block window
                current_start = current_end + timedelta(days=1)
                time.sleep(0.12)  # Small pacing pause

            if not all_candles:
                return None

            # Remove duplicates at chunk overlap boundaries
            df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df = df.drop_duplicates(subset=["timestamp"])
            
            df["date"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST).dt.tz_localize(None)
            df = df.drop(columns=["timestamp"])
            df = df.sort_values("date").reset_index(drop=True)

            return df

        except Exception as e:
            logger.error(f"Historical chunk aggregation error for {symbol}: {e}")
            return None

    def get_quote(self, symbol: str) -> Optional[dict]:
        """Get current live quote for a symbol."""
        if not self.fyers:
            return None

        try:
            response = self.fyers.quotes(data={"symbols": symbol})
            if response.get("s") != "ok":
                return None

            quote = response["d"][0]["v"]
            return {
                "symbol": symbol,
                "ltp": quote.get("lp"),
                "open": quote.get("open_price"),
                "high": quote.get("high_price"),
                "low": quote.get("low_price"),
                "close": quote.get("prev_close_price"),
                "volume": quote.get("volume"),
                "timestamp": datetime.now(IST),
            }

        except Exception as e:
            logger.error(f"Quote error for {symbol}: {e}")
            return None

    def get_positions(self) -> list:
        if not self.fyers:
            return []

        try:
            response = self.fyers.positions()
            if response.get("s") != "ok":
                return []
            return response.get("netPositions", [])

        except Exception as e:
            logger.error(f"Positions error: {e}")
            return []

    def get_funds(self) -> Optional[float]:
        """Get available cash balance."""
        if not self.fyers:
            return None

        try:
            response = self.fyers.funds()
            if response.get("s") != "ok":
                return None

            for item in response.get("fund_limit", []):
                if item.get("title") == "Available Balance":
                    return float(item.get("equityAmount", 0))
            return None

        except Exception as e:
            logger.error(f"Funds error: {e}")
            return None

    # =============================================================================
    # # Throttled Universe Pipeline
    # =============================================================================

    def fetch_universe_data(self, symbols: list, days: int = 220) -> dict:
        """
        Fetch historical data for all universe symbols sequentially.
        Implements high-accuracy rate throttling to bypass broker firewalls.
        """
        result = {}
        total_symbols = len(symbols)
        
        logger.info(f"⏳ Onlining throttled intake module for {total_symbols} watchlist candidates...")
        
        for idx, sym in enumerate(symbols):
            try:
                # Removed the "INDEX" block so NIFTY50 can be processed successfully
                if "FOOTWEAR" in sym:
                    continue
                
                # Fetch DAILY intervals to match the institutional architecture
                df = self.get_historical(sym, days=days, resolution="D")
                
                if df is not None and len(df) >= 50:
                    result[sym] = df
                else:
                    logger.debug(f"Skipping {sym} - insufficient chronological array length.")
                
                # 150ms micro-pause holds the polling execution frequency safely beneath exchange volume ceilings
                time.sleep(0.15)
                
                if (idx + 1) % 25 == 0:
                    logger.info(f"📊 Download Progression Status: {idx + 1}/{total_symbols} processed.")
                    
            except Exception as e:
                logger.error(f"Disruption mapped on asset processing array loop for {sym}: {e}")
                time.sleep(0.5)  
                continue

        logger.info(f"✅ Download Matrix Consolidated: {len(result)}/{total_symbols} symbols mapped.")
        return result


# Singleton Initialization Pattern
fyers_client = FyersClient()