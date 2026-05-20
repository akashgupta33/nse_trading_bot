#!/usr/bin/env bash
set -e

# entrypoint.sh - Auto-auth before starting the agent
# This runs inside the Docker container to ensure token is fresh

echo "$(date '+%Y-%m-%d %H:%M:%S') | INFO | entrypoint: Starting Fyers auto-auth..."

# Try headless TOTP first (silent mode)
if python auto_auth.py --mode totp 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUCCESS | entrypoint: TOTP auto-auth successful"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') | WARNING | entrypoint: TOTP auth failed, trying Telegram fallback..."
    if python auto_auth.py --mode telegram; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') | SUCCESS | entrypoint: Telegram auth successful"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') | ERROR | entrypoint: Both auth modes failed - agent will not start"
        exit 1
    fi
fi

# Verify token works
echo "$(date '+%Y-%m-%d %H:%M:%S') | INFO | entrypoint: Verifying Fyers connection..."
if python auto_auth.py --test; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUCCESS | entrypoint: Fyers connection verified. Starting agent..."
    exec python agent.py
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') | ERROR | entrypoint: Fyers connection verification failed"
    exit 1
fi
