"""
auto_auth.py - Automated daily Fyers token refresh.

Fyers tokens expire every day at midnight. This script automates
the auth flow using the TOTP secret (if you have 2FA) or by
using the Fyers API v3 client_credentials flow.

TWO MODES:
  Mode A (Recommended): Client credentials flow - no browser needed.
                       Works if your Fyers app allows it.
  Mode B (Fallback):    Telegram-based auth - bot sends you the login
                       link, you click it once, paste the code back.

Setup:
  1. Add FYERS_TOTP_SECRET to .env (from Fyers 2FA setup)
     OR set FYERS_PIN in .env (your Fyers login PIN)
  2. Scheduled natively in scheduler.py to run at 8:00 AM Mon-Fri.
"""

import os
import sys
import json
import argparse
import hashlib
import struct
import hmac
import time
import requests
from datetime import datetime
from pathlib import Path
import pytz
from loguru import logger
from dotenv import load_dotenv

# Load .env file explicitly
load_dotenv()

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import settings

IST = pytz.timezone("Asia/Kolkata")
TOKEN_PATH = Path("config/.fyers_token")

# =============================================================================
# # Mode A: Automated TOTP-based auth (fully headless)
# =============================================================================
def get_totp_code(secret: str) -> str:
    """Generate TOTP code from secret (same as Google Authenticator)."""
    import base64
    key = base64.b32decode(secret.upper().replace(" ", ""), casefold=True)
    t = int(time.time()) // 30
    msg = struct.pack(">Q", t)
    h = hmac.new(key, msg, "sha1").digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1000000).zfill(6)


def auto_auth_totp() -> bool:
    """Headless automated auth using Fyers credentials + TOTP."""
    client_id = settings.fyers_client_id
    app_id = os.getenv("FYERS_APP_ID", client_id.split("-")[0])
    pin = os.getenv("FYERS_PIN", "")  
    pan_or_dob = os.getenv("FYERS_PAN_DOB", "")
    totp_secret = os.getenv("FYERS_TOTP_SECRET", "")
    redirect_uri = settings.fyers_redirect_uri
    secret_key = settings.fyers_secret_key

    if not all([pin, pan_or_dob, totp_secret]):
        logger.debug("TOTP credentials missing in .env. Skipping Mode A.")
        return False

    try:
        logger.info("Attempting Mode A: Headless TOTP Login...")
        session = requests.Session()
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json"
        }

        # Step 1: Send client_id + PIN
        payload1 = {"fy_id": pan_or_dob, "app_id": app_id, "password": pin}
        r = session.post("https://api-t1.fyers.in/vagator/v2/send_login_otp_v2", json=payload1, headers=headers, timeout=10)
        
        if r.status_code != 200 or not r.json().get("s") == "ok":
            logger.error(f"Step 1 failed: {r.text[:200]}")
            return False

        request_key = r.json().get("request_key", "")

        # Step 2: TOTP verification
        totp_code = get_totp_code(totp_secret)
        payload2 = {"request_key": request_key, "otp": totp_code}
        r = session.post("https://api-t1.fyers.in/vagator/v2/verify_otp", json=payload2, headers=headers, timeout=10)

        if r.status_code != 200 or not r.json().get("s") == "ok":
            logger.error(f"Step 2 TOTP failed: {r.text[:200]}")
            return False

        request_key = r.json().get("request_key", r.json().get("request_key"))

        # Step 3: PIN verification
        pin_hash = hashlib.sha256(pin.encode()).hexdigest()  
        payload3 = {"request_key": request_key, "identity_type": "pin", "identifier": pin_hash}
        r = session.post("https://api-t1.fyers.in/vagator/v2/verify_pin_v2", json=payload3, headers=headers, timeout=10)

        if r.status_code != 200 or not r.json().get("s") == "ok":
            logger.error(f"Step 3 PIN failed: {r.text[:200]}")
            return False

        access_token_temp = r.json().get("data", {}).get("access_token", "")

        # Step 4: Get auth code
        payload4 = {
            "fyers_id": pan_or_dob,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "appType": "100",
            "code_challenge": "",
            "state": "auto_auth",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True,
        }
        
        headers_auth = headers.copy()
        headers_auth["Authorization"] = f"Bearer {access_token_temp}"
        r = session.post("https://api.fyers.in/api/v3/token", json=payload4, headers=headers_auth, timeout=10)

        if r.status_code != 302:
            uri = r.json().get("location", "")
            auth_code = _extract_auth_code(uri)
            if not auth_code and r.status_code == 200:
                data = r.json()
                auth_code = data.get("auth_code") or _extract_auth_code(data.get("Url", ""))
        else:
            location = r.headers.get("location", "")
            auth_code = _extract_auth_code(location)

        if not auth_code:
            logger.error(f"Could not extract auth_code. Response: {r.text[:300]}")
            return False

        # Step 5: Exchange for access token
        from fyers_apiv3.fyersModel import SessionModel
        sess = SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        sess.set_token(auth_code)
        resp = sess.generate_token()

        if resp.get("s") != "ok":
            logger.error(f"Token generation failed: {resp}")
            return False

        token = resp["access_token"]
        _save_token(token)
        logger.success(f"TOTP auto-auth successful at {datetime.now(IST).strftime('%H:%M IST')}")
        return True

    except Exception as e:
        logger.error(f"TOTP auto-auth error: {e}")
        return False


def _extract_auth_code(url: str) -> str:
    """Extract auth_code from redirect URL."""
    if not url:
        return ""
    if "auth_code=" in url:
        return url.split("auth_code=")[1].split("&")[0]
    if "code=" in url:
        return url.split("code=")[1].split("&")[0]
    return ""


def _save_token(token: str):
    """Saves token with strict IST timezone to prevent midnight rollover bugs."""
    TOKEN_PATH.parent.mkdir(exist_ok=True)
    data = {"token": token, "date": datetime.now(IST).strftime("%Y-%m-%d")}
    TOKEN_PATH.write_text(json.dumps(data))
    logger.info(f"Token saved to {TOKEN_PATH} for date: {data['date']}")


# =============================================================================
# # Mode B: Telegram-assisted auth (semi-automated fallback)
# =============================================================================
def telegram_auth_request() -> bool:
    import http.server
    import urllib.parse
    from fyers_apiv3.fyersModel import SessionModel

    logger.info("Attempting Mode B: Telegram Manual Auth...")
    sess = SessionModel(
        client_id=settings.fyers_client_id,
        secret_key=settings.fyers_secret_key,
        redirect_uri=settings.fyers_redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )
    auth_url = sess.generate_authcode()

    msg = (
        f"<b>🔑 Fyers daily re-auth needed</b>\n\n"
        f"🔗 <a href='{auth_url}'>Click this link to login</a>\n"
        f"<i>* After login you'll be redirected to localhost - that's normal, </i>\n"
        f"<i>the agent will capture the code automatically.</i>"
    )
    _send_telegram(msg)
    logger.info("Auth URL sent to Telegram. Waiting for callback...")

    auth_code_holder = [None]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get("auth_code", params.get("code", [None]))[0]
            if code:
                auth_code_holder[0] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1 style='color:green;'>Auth captured! Return to terminal.</h1>")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = b"""
            <html>
                <body>
                    <h1>Completing authentication...</h1>
                    <script>
                        var code = new URLSearchParams(window.location.search).get('auth_code') || 
                                   new URLSearchParams(window.location.hash.substring(1)).get('auth_code');
                        if (code) {
                            fetch('/_capture', {method: 'POST', body: JSON.stringify({code: code})})
                            .then(()=> document.body.innerHTML = '<h1 style="color:green;">Auth captured! Close this tab.</h1>');
                        }
                    </script>
                </body>
            </html>
            """
            self.wfile.write(html)

        def log_message(self, format, *args):
            pass 

        def do_POST(self):
            if self.path != '/_capture': return
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length)) if length else {}
            code = data.get('code')
            if code:
                auth_code_holder[0] = code
                self.send_response(200)
                self.end_headers()

    server = http.server.HTTPServer(("0.0.0.0", 8080), Handler)
    server.timeout = 300 
    
    logger.info("Waiting up to 5 minutes for Fyers callback on port 8080...")
    server.handle_request()

    auth_code = auth_code_holder[0]
    if not auth_code:
        logger.error("No auth code received within timeout.")
        return False

    sess.set_token(auth_code)
    resp = sess.generate_token()
    if resp.get("s") != "ok":
        return False

    token = resp["access_token"]
    _save_token(token)
    _send_telegram("✅ <b>Fyers re-auth successful!</b> Agent is ready for today.")
    return True


# =============================================================================
# # Helper Functions
# =============================================================================
def _send_telegram(msg: str):
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass


def is_token_fresh() -> bool:
    """Checks if the token file exists and was created today."""
    if not TOKEN_PATH.exists():
        return False
    try:
        data = json.loads(TOKEN_PATH.read_text())
        today = datetime.now(IST).strftime("%Y-%m-%d")
        return data.get("date") == today and bool(data.get("token"))
    except Exception:
        return False


def verify_token() -> bool:
    """Quick check that the saved token is valid with the broker."""
    try:
        from data.fyers_client import fyers_client
        ok = fyers_client.connect()
        if ok:
            logger.success("Token verified - Fyers connection healthy")
            return True
        return False
    except Exception as e:
        logger.error(f"Verify errors: {e}")
        return False


# =============================================================================
# # Unified Entry Point
# =============================================================================
def generate_token() -> bool:
    """
    Intelligent logic flow:
    1. Check for today's file -> Ping Broker.
    2. Attempt Headless TOTP login.
    3. Fallback to Telegram manual URL.
    """
    logger.info(f"Auto-auth pipeline triggered | {datetime.now(IST).strftime('%H:%M IST')}")

    # 1. The Cache Check (Prevents Docker restart loops)
    if is_token_fresh():
        logger.info("Local token found for today. Pinging broker to verify...")
        if verify_token():
            logger.success("Valid active token confirmed. Skipping re-authentication.")
            return True

    # 2. Mode A (Headless)
    success = auto_auth_totp()
    
    # 3. Mode B (Manual Fallback)
    if not success:
        logger.warning("TOTP auth bypassed or failed. Escalating to Telegram...")
        success = telegram_auth_request()

    # 4. Final System Check
    if success:
        return verify_token()
    else:
        msg = "❌ <b>Fyers auto-auth FAILED</b> - System locked out of market data."
        logger.error(msg)
        _send_telegram(msg)
        return False


# =============================================================================
# # Terminal Execution (Used by entrypoint.sh)
# =============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-authenticate with Fyers")
    parser.add_argument("--test", action="store_true", help="Only verify existing token")
    args = parser.parse_args()

    if args.test:
        sys.exit(0 if verify_token() else 1)

    # This is the line that actually runs the smart logic when Docker boots
    sys.exit(0 if generate_token() else 1)