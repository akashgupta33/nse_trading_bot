#!/usr/bin/env bash
set -e

# entrypoint.sh - Auto-auth before starting the agent
# This runs inside the Docker container to ensure token is fresh

echo "$(date '+%Y-%m-%d %H:%M:%S') | INFO | entrypoint: Checking Fyers token..."

# Check if valid token already exists (first priority)
if python auto_auth.py --test 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUCCESS | entrypoint: Valid token found, skipping auth"
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUCCESS | entrypoint: Starting agent..."
    exec python agent.py
fi

# No valid token - attempt full auto-auth flow (TOTP first, then Telegram fallback)
echo "$(date '+%Y-%m-%d %H:%M:%S') | WARNING | entrypoint: No valid token, attempting auto-auth..."
if python auto_auth.py 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUCCESS | entrypoint: Auto-auth successful"
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUCCESS | entrypoint: Starting agent..."
    exec python agent.py
fi

# Auto-auth failed - stop startup so the issue can be resolved manually
echo "$(date '+%Y-%m-%d %H:%M:%S') | ERROR | entrypoint: Auto-auth failed. Container will exit."
exit 1
