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

# No valid token - try TOTP (headless, works in Docker)
echo "$(date '+%Y-%m-%d %H:%M:%S') | WARNING | entrypoint: No valid token, attempting TOTP auth..."
if python auto_auth.py --mode totp 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUCCESS | entrypoint: TOTP auto-auth successful"
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUCCESS | entrypoint: Starting agent..."
    exec python agent.py
fi

# TOTP failed (missing config) - proceed with current token and log warning
echo "$(date '+%Y-%m-%d %H:%M:%S') | WARNING | entrypoint: TOTP auth not configured or failed"
echo "$(date '+%Y-%m-%d %H:%M:%S') | INFO | entrypoint: Proceeding with existing token (if available)..."
exec python agent.py
