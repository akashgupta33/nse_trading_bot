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
  2. Schedule this script to run at 8:50 AM Mon-Fri

Usage:
  python auto_auth.py        # run manually
  python auto_auth.py --test  # verify token works without trading
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
    # Decode base32 secret
    key = base64.b32decode(secret.upper().replace(" ", ""), casefold=True)
    # Current 30-second interval
    t = int(time.time()) // 30
    msg = struct.pack(">Q", t)
    # HMAC-SHA1
    h = hmac.new(key, msg, "sha1").digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1000000).zfill(6)


def auto_auth_totp() -> bool:
    """Headless automated auth using Fyers credentials + TOTP."""
    logger.info("Starting Step 1 - Client login")
    client_id = settings.fyers_client_id
    
    # App ID is the part before the hyphen in client_id e.g. "XYZ123" from "XYZ123-100"
    app_id = os.getenv("FYERS_APP_ID", client_id.split("-")[0])
    pin = os.getenv("FYERS_PIN", "")  # Changed from PASSWORD to PIN
    pan_or_dob = os.getenv("FYERS_PAN_DOB", "")
    totp_secret = os.getenv("FYERS_TOTP_SECRET", "")
    redirect_uri = settings.fyers_redirect_uri
    secret_key = settings.fyers_secret_key

    if not all([pin, pan_or_dob, totp_secret]):
        logger.warning("TOTP auto-auth needs FYERS_PIN, FYERS_PAN_DOB, FYERS_TOTP_SECRET in .env")
        return False

    try:
        session = requests.Session()
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json"
        }

        # Step 1: Send client_id + PIN (as password field in API)
        payload1 = {
            "fy_id": pan_or_dob,
            "app_id": app_id,
            "password": pin  # Fyers API accepts PIN in password field
        }
        r = session.post("https://api-t1.fyers.in/vagator/v2/send_login_otp_v2", json=payload1, headers=headers, timeout=10)
        
        if r.status_code != 200 or not r.json().get("s") == "ok":
            logger.error(f"Step 1 failed: {r.text[:200]}")
            return False

        request_key = r.json().get("request_key", "")

        # Step 2: TOTP verification
        logger.info("TOTP auth: Step 2 - TOTP verification")
        totp_code = get_totp_code(totp_secret)
        logger.debug(f"Generated TOTP: {totp_code}")

        payload2 = {
            "request_key": request_key,
            "otp": totp_code
        }
        r = session.post("https://api-t1.fyers.in/vagator/v2/verify_otp", json=payload2, headers=headers, timeout=10)

        if r.status_code != 200 or not r.json().get("s") == "ok":
            logger.error(f"Step 2 TOTP failed: {r.text[:200]}")
            return False

        request_key = r.json().get("request_key", r.json().get("request_key"))

        # Step 3: PIN verification
        logger.info("IDIP auth: Step 3 - PIN verification")
        pin_hash = hashlib.sha256(pin.encode()).hexdigest()  # Hash the actual PIN
        
        payload3 = {
            "request_key": request_key,
            "identity_type": "pin",
            "identifier": pin_hash
        }
        r = session.post("https://api-t1.fyers.in/vagator/v2/verify_pin_v2", json=payload3, headers=headers, timeout=10)

        if r.status_code != 200 or not r.json().get("s") == "ok":
            logger.error(f"Step 3 PIN failed: {r.text[:200]}")
            return False

        access_token_temp = r.json().get("data", {}).get("access_token", "")

        # Step 4: Get auth code
        logger.info("IDIP auth: Step 4 - getting auth code")
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
            # Try parsing auth code from URL
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

        logger.info(f"Got auth_code: {auth_code[:8]}...")

        # Step 5: Exchange for access token
        logger.info("TOTP auth: Step 5 - exchanging for access token")
        from fyers_apiv3 import fyersModel
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
    TOKEN_PATH.parent.mkdir(exist_ok=True)
    data = {"token": token, "date": datetime.now().strftime("%Y-%m-%d")}
    TOKEN_PATH.write_text(json.dumps(data))
    logger.info(f"Token saved to {TOKEN_PATH}")


# =============================================================================
# # Mode B: Telegram-assisted auth (semi-automated fallback)
# =============================================================================
def telegram_auth_request() -> bool:
    """
    Sends the auth URL to your Telegram.
    You click the link, login, and the agent catches the redirect
    via a tiny local HTTP server.
    """
    import http.server
    import threading
    import urllib.parse
    from fyers_apiv3.fyersModel import SessionModel

    sess = SessionModel(
        client_id=settings.fyers_client_id,
        secret_key=settings.fyers_secret_key,
        redirect_uri=settings.fyers_redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )
    auth_url = sess.generate_authcode()

    # Log the auth URL so it can be opened manually if Telegram click fails
    logger.info(f"Fyers auth URL: {auth_url}")

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
                                self.wfile.write(b"<h1>Auth successful! Return to terminal.</h1>")
                                return

                        # If code not in query (some providers return it in fragment),
                        # serve a tiny HTML page that extracts the fragment and POSTs it back.
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.end_headers()
                        html = b"""
<html>
    <head><meta charset="utf-8"><title>Auth callback</title></head>
    <body>
        <h1>Completing authentication...</h1>
        <p>If your browser doesn't return to the app, click the button below.</p>
        <button id="send">Send auth to app</button>
        <script>
            function postBody(body) {
                fetch('/_capture', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(()=>{
                    document.body.innerHTML = '<h1>Auth captured. You can close this tab.</h1>';
                }).catch(()=>{
                    document.body.innerHTML = '<h1>Failed to send auth to app. Please copy the URL and paste it to the terminal.</h1>';
                });
            }
            (function(){
                try {
                    var params = new URLSearchParams(window.location.search);
                    var code = params.get('auth_code') || params.get('code');
                    if (code) { postBody({code: code}); return; }
                    // Try fragment
                    var frag = window.location.hash.substring(1);
                    var fragParams = new URLSearchParams(frag);
                    var fcode = fragParams.get('auth_code') || fragParams.get('code');
                    if (fcode) { postBody({code: fcode}); return; }
                } catch (e) { }
                document.getElementById('send').addEventListener('click', function(){
                    // attempt to send whatever we can
                    var frag = window.location.hash.substring(1);
                    var fragParams = new URLSearchParams(frag);
                    var fcode = fragParams.get('auth_code') || fragParams.get('code') || null;
                    postBody({code: fcode, url: window.location.href});
                });
            })();
        </script>
    </body>
</html>
"""
                        self.wfile.write(html)

        def log_message(self, format, *args):
            pass  # Suppress server logs

        def do_POST(self):
            # Capture JSON POSTs from the HTML page's JS with the auth code
            if self.path != '/_capture':
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b''
            try:
                data = json.loads(raw.decode('utf-8')) if raw else {}
            except Exception:
                data = {}
            code = data.get('code') or ''
            # Also accept full URL fallback
            if not code and data.get('url'):
                code = _extract_auth_code(data.get('url'))
            if code:
                auth_code_holder[0] = code
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<h1>Auth captured. Return to the app.</h1>')
            else:
                self.send_response(400)
                self.end_headers()

    server = http.server.HTTPServer(("0.0.0.0", 8080), Handler)
    server.timeout = 300  # 5 minutes to complete login
    
    logger.info("Waiting up to 5 minutes for Fyers callback on port 8080...")
    server.handle_request()

    auth_code = auth_code_holder[0]
    if not auth_code:
        logger.error("No auth code received within timeout.")
        return False

    sess.set_token(auth_code)
    resp = sess.generate_token()
    if resp.get("s") != "ok":
        logger.error(f"Token generation failed: {resp}")
        return False

    token = resp["access_token"]
    _save_token(token)
    _send_telegram("✅ <b>Fyers re-auth successful!</b> Agent is ready for today.")
    logger.success("Telegram-assisted auth successful")
    return True


def _send_telegram(msg: str):
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "timeout": 5}
        requests.post(url, json=payload)
    except Exception:
        pass


# =============================================================================
# # Verify token works
# =============================================================================
def verify_token() -> bool:
    """Quick check that the saved token is valid."""
    try:
        from data.fyers_client import fyers_client
        ok = fyers_client.connect()
        if ok:
            logger.success("Token verified - Fyers connection healthy")
            return True
        else:
            logger.error("Token verification failed")
            return False
    except Exception as e:
        logger.error(f"Verify errors: {e}")
        return False


# =============================================================================
# # Entry point
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Auto-authenticate with Fyers")
    parser.add_argument("--test", action="store_true", help="Only verify existing token")
    parser.add_argument("--mode", choices=["totp", "telegram"], default="totp",
                        help="Auth modes: totp (headless) or telegram (semi-auto)")
    args = parser.parse_args()

    if args.test:
        ok = verify_token()
        sys.exit(0 if ok else 1)

    logger.info(f"Auto-auth starting | mode={args.mode} | {datetime.now(IST).strftime('%H:%M IST')}")

    success = False
    if args.mode == "totp":
        success = auto_auth_totp()
        if not success:
            logger.warning("TOTP auth failed - falling back to Telegram mode")
            success = telegram_auth_request()
    else:
        success = telegram_auth_request()

    if success:
        verify_token()
        sys.exit(0)
    else:
        msg = "❌ <b>Fyers auto-auth FAILED</b> - agent will not trade today"
        logger.error(msg)
        _send_telegram(msg)
        sys.exit(1)


if __name__ == "__main__":
    main()