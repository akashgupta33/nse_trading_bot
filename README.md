# NSE Trading Agent

## Daily auto-auth flow

This app supports daily Fyers token refresh using `auto_auth.py`.

### How it works

- `entrypoint.sh` checks whether the cached token in `config/.fyers_token` is still valid.
- If the token is missing or expired, it runs `auto_auth.py`.
- `auto_auth.py` attempts headless renewal first, then falls back to Telegram-assisted auth.
- `agent.py` schedules a daily Fyers token refresh job before market open.

### Docker deployment

For token persistence, mount the `config/` folder from the host into the container:

```bash
docker build -t nse_trading_agent:latest .
docker run -d -p 8080:8080 \
  --env-file .env \
  -v "$(pwd)/config:/app/config" \
  --name nse_trading_agent \
  nse_trading_agent:latest
```

The mounted `config/` folder keeps the generated `config/.fyers_token` across container restarts.

### Scheduling

If you run the agent as a long-lived service, it will refresh the Fyers token daily at `07:20 IST` on trading days.

If you prefer host cron instead, schedule the following command each weekday:

```bash
cd /path/to/NSE_Trading_Agent
/docker/path/python3 auto_auth.py --mode telegram
```

### Notes

- Fyers tokens are valid only for one day, so daily refresh is expected.
- The Telegram fallback still requires one click when a new auth link is generated.
- If you want fully headless renewal, store `FYERS_PASSWORD`, `FYERS_PAN_DOB`, and `FYERS_TOTP_SECRET` in `.env`.
