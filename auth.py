"""
Run this ONCE to authenticate with Fyers and cache the token.
After this, the agent uses the cached token daily.

Usage:
    python auth.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.fyers_client import fyers_client
from loguru import logger

def main():
    logger.info("Fyers authentication flow starting...")
    print("\n" + "="*50)
    print("FYERS AUTHENTICATION")
    print("="*50)
    print("Make sure your .env file has:")
    print("  FYERS_CLIENT_ID")
    print("  FYERS_SECRET_KEY")
    print("  FYERS_REDIRECT_URI")
    print("="*50 + "\n")
    
    success = fyers_client.authenticate()
    if success:
        print("\n✅ Authentication successful! Token saved.")
        print("You can now run: python agent.py")
    else:
        print("\n❌ Authentication failed. Check your credentials in .env")
        sys.exit(1)

if __name__ == "__main__":
    main()