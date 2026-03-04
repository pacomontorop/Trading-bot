# CLAUDE.md — Trading-bot System Guide

## Project Overview

Long-only equity trading bot using Alpaca (paper/live), QuiverQuant signals, and Yahoo Finance data.
Designed for automated bracket-order execution with ATR-based risk management.

## Architecture

```
main.py / start.py              ← entry points
core/scheduler.py               ← main trading loop (60-second tick)
signals/reader.py               ← signal aggregation & scoring
core/risk_manager.py            ← paper account position sizing, daily limits
core/executor.py                ← paper account order placement
core/position_protector.py      ← paper account trailing-stop & break-even
broker/alpaca.py                ← lazy Alpaca REST wrapper (paper account)

core/live_risk_manager.py       ← live account position sizing, daily limits
core/live_executor.py           ← live account order placement & protection
broker/alpaca_live.py           ← lazy Alpaca REST wrapper (live account)

config/policy.yaml              ← all tunable parameters (no code changes needed)
.env                            ← secrets (never committed)
```

## Quick Start

```bash
# 1. Install dependencies
pip install --only-binary=:all: msgpack pytz numpy pandas aiohttp prometheus-client praw pytest pytest-mock python-dotenv
pip install --no-deps alpaca-trade-api yfinance pandas-market-calendars
pip install deprecation==2.1.0 websockets websocket-client pytz platformdirs peewee

# 2. Copy and fill in secrets
cp .env.example .env
# Edit .env — mandatory: APCA_API_KEY_ID, APCA_API_SECRET_KEY

# 3. Run (paper trading — safe)
python main.py

# 4. Run tests
python -m pytest tests/ -v
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `APCA_API_KEY_ID` | **YES** | — | Alpaca paper account API key |
| `APCA_API_SECRET_KEY` | **YES** | — | Alpaca paper account API secret |
| `APCA_API_BASE_URL` | no | paper URL | Paper URL by default |
| `APCA_API_KEY_ID_REAL` | if live | — | Alpaca **live** account API key |
| `APCA_API_SECRET_KEY_REAL` | if live | — | Alpaca **live** account API secret |
| `ENABLE_LIVE_TRADING` | no | false | Activate parallel live account trading |
| `DAILY_RISK_LIMIT` | no | -200 | Max daily loss in USD (negative) |
| `DRY_RUN` | no | false | Simulate without placing orders (both accounts) |
| `ENABLE_QUIVER` | no | true | Use QuiverQuant signals |
| `ENABLE_YAHOO` | no | true | Use Yahoo Finance data |
| `ENABLE_FMP` | no | false | Use Financial Modeling Prep |
| `ENABLE_SHORTS` | no | false | Enable short selling |
| `FMP_API_KEY` | if FMP | — | Financial Modeling Prep key |
| `QUIVER_API_KEY` | if Quiver | — | QuiverQuant API key |
| `REDIS_URL` | no | — | Redis URL for distributed cache |
| `MONITOR_INTERVAL` | no | 60 | Main loop interval in seconds |
| `TELEGRAM_BOT_TOKEN` | no | — | Telegram bot for alerts |
| `TELEGRAM_CHAT_ID` | no | — | Telegram chat for alerts |

## Key Policy Parameters (`config/policy.yaml`)

### Risk
- `risk.daily_max_spend_usd`: Maximum notional spend per day (default 800)
- `risk.daily_max_new_positions`: Max new trades per day (default 3)
- `risk.max_total_open_positions`: Max simultaneous positions (default 10)
- `risk.max_position_size_usd`: Single position cap (default 500)
- `risk.min_position_size_usd`: Minimum viable position (default 100)
- `risk.symbol_cooldown_days`: Days between re-trading same symbol (default 5)
- `risk.atr_k`: ATR multiplier for stop distance (default 2.0)

### Execution
- `execution.use_bracket`: Always use bracket orders (default true — **do not disable**)
- `execution.take_profit_atr_mult`: Take-profit distance in ATR multiples (default 3.0)
- `execution.trailing_stop_atr_mult`: Trailing stop in ATR multiples (default 2.0)
- `execution.min_rr_ratio`: Minimum reward/risk to accept trade (default 1.2)

### Market / Macro
- `market.global_kill_switch`: Hard stop — set `true` to halt all new orders immediately
- `market.vix_pause_threshold`: Pause new entries when CBOE VIX exceeds this level
  - `0` = disabled (default behaviour, no VIX check)
  - Typical levels: calm < 20 · elevated 20–28 · fear > 28 · crisis > 35
  - Existing positions continue to be protected (trailing stops, break-even) regardless
  - VIX is fetched from Yahoo Finance (`^VIX`) and cached for 10 minutes

### Safeguards
- `safeguards.enabled`: Must be `true` for orders to execute
- `safeguards.ttl_days`: Days safeguards stay active after `started_at_utc`
- `safeguards.started_at_utc`: ISO-8601 timestamp of last deployment/reset
- **Keep `started_at_utc` current** — if TTL expires, all orders are blocked

### Live Account (`live_account.*`)
Controls the parallel real-money account (activated via `ENABLE_LIVE_TRADING=true`).
Paper trading is completely unaffected by these settings.

- `live_account.max_cash_pct`: Fraction of live cash to deploy per session (default 0.20 = 20 %)
- `live_account.max_position_size_usd`: Hard cap per live position (default $200)
- `live_account.min_position_size_usd`: Minimum viable live position (default $50)
- `live_account.daily_max_new_positions`: Max new live trades per day (default 2)
- `live_account.max_total_open_positions`: Max simultaneous live positions (default 5)
- `live_account.symbol_cooldown_days`: Days before re-trading a symbol on live (default 5)

**How it works**: the scheduler reuses the same gate-approved signals from the paper scan.
For each approved signal a separate, conservatively sized plan is computed for the live
account using the limits above.  Live orders are placed on `https://api.alpaca.markets`.
Live daily state is persisted in `data/risk_state_live.json`.

## Common Issues & Fixes

### Orders blocked: `safeguards_inactive`
The safeguards TTL has expired. Update `config/policy.yaml`:
```yaml
safeguards:
  started_at_utc: "2026-02-22T00:00:00Z"   # update to today
  ttl_days: 30                               # or 0 to disable TTL
```

### Import error: `ValueError: Key ID must be given`
Set Alpaca credentials in `.env` before running. The `broker/alpaca.py` module
uses **lazy initialization** — API object is created on first use, not import.

### `ModuleNotFoundError: No module named 'alpaca_trade_api'`
`msgpack` must be installed as a binary wheel first:
```bash
pip install --only-binary=:all: msgpack
pip install --no-deps alpaca-trade-api
```

### Tests fail to collect
Run `python -m pytest tests/ -v` from the repo root. The `tests/conftest.py`
sets dummy Alpaca credentials so modules can be imported without real keys.

### `data/symbols.csv` missing
The scheduler auto-generates it on first run via `utils/generate_symbols_csv.py`.
Ensure the `data/` directory exists and `QUIVER_API_KEY` / Yahoo Finance are accessible.

## Running Tests

```bash
python -m pytest tests/ -v          # all tests
python -m pytest tests/ -v -k gate  # gate tests only
```

All tests are network-isolated (mocked). No real API keys needed for tests.

## Code Style

- Python 3.11+, `from __future__ import annotations` in every module
- All public functions have type hints
- Network calls wrapped in `try/except` with fallback logging
- No global state except broker singleton and policy config
- Policy changes: edit `config/policy.yaml` — never hardcode values

## Branch & Deployment

- Development branch: `claude/complete-system-improvements-QWF37`
- Paper trading URL: `https://paper-api.alpaca.markets`
- Live trading URL: `https://api.alpaca.markets` (set via `APCA_API_BASE_URL`)
- Entry point for production: `uvicorn start:app --host 0.0.0.0 --port 8080`
- Healthcheck: `GET /` returns `{"status": "ok"}`

## System Improvements Applied (2026-02-22)

1. **Lazy broker initialization** — `broker/alpaca.py` now defers Alpaca REST
   client creation to first use, allowing test collection without API keys.
2. **Test fixtures** — `tests/conftest.py` sets dummy credentials so all
   tests can be collected and run without real API keys.
3. **Safeguards TTL reset** — `config/policy.yaml` `started_at_utc` updated
   to current date so orders are no longer blocked by expired TTL.
4. **Requirements pinned** — `requirements.txt` lists exact working versions
   for this Python 3.11 environment.
5. **`.env.example` expanded** — documents all environment variables.
6. **VIX fear gate** — `core/market_gate.get_vix_level()` pauses new entries
   when CBOE VIX exceeds `market.vix_pause_threshold` (default 28).
   Existing positions always continue to be protected.
7. **Parallel live account trading** — `broker/alpaca_live.py`,
   `core/live_risk_manager.py`, and `core/live_executor.py` add an optional
   real-money trading pass in the same scheduler loop.  Activated by setting
   `ENABLE_LIVE_TRADING=true` with `APCA_API_KEY_ID_REAL`/`APCA_API_SECRET_KEY_REAL`
   in the environment.  Paper trading remains fully operational regardless.
