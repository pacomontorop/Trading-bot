# CLAUDE.md — Trading-bot Reference

> **Audience**: Any AI assistant or developer picking up this codebase.
> This document is the single authoritative reference for architecture, data flow, configuration, known issues, and development conventions.

---

## 1. System Overview

**Long-only, event-driven equity trading bot** using:

| Layer | Role |
|-------|------|
| **Alpaca** | Broker — order execution, account data, positions |
| **Yahoo Finance** | Price data, ATR, market cap, volume — filter/context only |
| **Quiver Quant PRO** | Primary alpha source — alternative data signals |

**Philosophy**: Buy on strong alternative-data events (insider buys, gov contracts, patent momentum). Filter noise by liquidity. Never average down. Never move a stop downward. Always protect gains dynamically.

---

## 2. Repository Layout

```
Trading-bot/
├── main.py                    # Entry point → equity_scheduler_loop()
├── start.py                   # FastAPI server + scheduler in daemon thread
├── config.py                  # Global flags loaded from env + policy.yaml
├── config/
│   └── policy.yaml            # ★ SINGLE SOURCE OF TRUTH for all thresholds
├── core/
│   ├── scheduler.py           # Main loop: market check → protect → scan → order
│   ├── executor.py            # place_long_order() — bracket order submission
│   ├── risk_manager.py        # DailyRiskState, plan_trades(), check_risk_limits()
│   ├── order_protection.py    # compute_bracket_prices(), compute_break_even_stop()
│   ├── safeguards.py          # run_safeguards() — break-even + trailing protection
│   ├── position_protector.py  # tick_protect_positions() — threaded runner every 60s
│   ├── market_gate.py         # is_us_equity_market_open()
│   ├── broker.py              # get_tick_size(), round_to_tick()
│   └── order_utils.py         # client_order_id utilities
├── broker/
│   ├── alpaca.py              # Alpaca REST API wrapper (api object, list_positions, etc.)
│   └── account.py             # get_account_equity_safe() with 24h fallback cache
├── signals/
│   ├── reader.py              # ★ get_top_signals() — full scan pipeline (684 lines)
│   ├── features.py            # get_symbol_features() — flat numeric feature dict
│   ├── scoring.py             # fetch_yahoo_snapshot() — YF data + ATR calculation
│   ├── quiver_ingest.py       # Raw Quiver API calls + dual-layer cache
│   ├── quiver_utils.py        # Raw API → numeric features (per-endpoint extractors)
│   ├── quiver_approval.py     # Legacy compatibility shim
│   ├── quiver_throttler.py    # Rate-limit throttling for Quiver requests
│   ├── filters.py             # is_position_open() — cached position check
│   └── gates.py               # Long trade safety gates (asset class, etc.)
├── data/
│   ├── symbols.csv            # Trading universe: canonical, yahoo, quiver columns
│   ├── providers.py           # Multi-provider price cascade (Alpaca→Yahoo→FMP)
│   └── tiingo_client.py       # Tiingo API client
├── utils/
│   ├── logger.py              # log_event(msg, event=) — structured prefix logging
│   ├── universe.py            # load_universe() — parses symbols.csv
│   ├── symbols.py             # normalize_for_yahoo(), detect_asset_class()
│   ├── generate_symbols_csv.py# Generates symbols.csv from Alpaca assets list
│   ├── cache.py               # In-memory TTL cache (get/set)
│   ├── persistent_cache.py    # File-backed TTL cache (get/set)
│   ├── daily_risk.py          # Daily risk state helpers
│   ├── market_calendar.py     # NYSE calendar fallback
│   ├── telegram_alert.py      # Telegram notifications
│   └── health.py              # Scan metrics tracking
└── tests/
    ├── test_bracket_payload.py
    └── test_gate_selfcheck.py
```

---

## 3. Configuration — `config/policy.yaml`

**All thresholds live here. Never hardcode values elsewhere.**

```yaml
signals:
  freshness_days_quiver: 7          # Max age for Quiver data to be considered
  freshness_days_yahoo_prices: 2    # Max age of Yahoo price history

market:
  global_kill_switch: false         # Set true to halt all trading immediately

yahoo_gate:                         # STRICT mode (default)
  min_avg_volume_7d: 250000
  min_market_cap: 1000000000        # $1B
  max_atr_pct: 6.0                  # ATR/price * 100
  require_trend_positive: false
  min_price: 5.0
  max_price: 1000.0
  # RELAXED mode (activated by fast lane):
  relaxed_min_market_cap: 300000000
  relaxed_min_avg_volume_7d: 50000
  relaxed_max_atr_pct: 12.0

quiver_gate:
  enabled: true
  fast_lane_enabled: true
  # Fast lane triggers (bypass strict Yahoo gate):
  insider_buy_strong_min_count_7d: 2
  gov_contract_strong_min_total_30d: 1000000
  patent_momentum_min_strong: 1.0
  # Minimum gate thresholds (0 = disabled for that check):
  insider_buy_min_count_lookback: 1
  gov_contract_min_total_amount: 0
  gov_contract_min_count: 0
  patent_momentum_min: 0
  sec13f_count_min: 0
  sec13f_change_min_pct: 0

risk:
  daily_max_spend_usd: 800
  daily_max_new_positions: 3
  max_total_open_positions: 10
  max_position_size_usd: 500
  min_position_size_usd: 100
  max_exposure_pct_equity: 0.60
  max_symbol_exposure_pct_equity: 0.12
  cash_buffer_pct: 0.10
  symbol_cooldown_days: 5
  if_position_open_skip: true
  skip_if_order_pending: true
  slippage_buffer_pct: 0.002
  max_symbol_risk_pct: 0.01         # Risk 1% of equity per trade
  atr_k: 2.0                        # Stop = max(ATR×2, price×5%)
  min_stop_pct: 0.05
  stop_limit_buffer_pct: 0.002

execution:
  use_bracket: true
  take_profit_atr_mult: 3.0         # TP = entry + ATR×3
  trailing_stop_atr_mult: 2.0
  time_in_force: day
  allow_partial_fills: true
  min_rr_ratio: 1.2

safeguards:
  enabled: true
  ttl_days: 7                        # ⚠️ MUST KEEP CURRENT (see §9)
  started_at_utc: "2026-01-29T00:00:00Z"  # ⚠️ EXPIRED — update to today
  break_even_R: 1.0                  # Move stop to BE when price = entry + 1R
  break_even_buffer_pct: 0.001
  trailing_enable: true

cache:
  quiver_heavy_ttl_sec: 86400       # 24h cache for bulk Quiver endpoints
  symbol_ttl_sec: 600               # 10min cache for per-symbol feature results
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `APCA_API_KEY_ID` | — | **Required** Alpaca API key |
| `APCA_API_SECRET_KEY` | — | **Required** Alpaca secret |
| `QUIVER_API_KEY` | — | **Required for signals** Quiver PRO key |
| `ENABLE_QUIVER` | `true` | Enable Quiver data source |
| `ENABLE_YAHOO` | `true` | Enable Yahoo Finance |
| `ENABLE_FMP` | `false` | Enable FMP fallback |
| `DRY_RUN` | `false` | Log orders but don't submit |
| `STRICT_GATES` | `false` | Force strict Yahoo + Quiver thresholds |
| `APCA_API_BASE_URL` | paper URL | Set to live URL for real trading |
| `QUIVER_TIMEOUT` | `30` | Request timeout in seconds |
| `REDIS_URL` | — | Enable Redis (optional) |

---

## 4. Full Data Flow — Step by Step

```
START
  │
  ▼
[scheduler.py] equity_scheduler_loop()
  │
  ├─ 1. SYMBOLS: load data/symbols.csv (or regenerate from Alpaca)
  │
  ├─ 2. LOOP every 60 seconds:
  │      a. is_us_equity_market_open()? → if NO: sleep, continue
  │      b. tick_protect_positions()   → break-even + trailing on open positions
  │      c. get_top_signals()          → full scan pipeline (see below)
  │      d. for each approved signal:
  │           - is_position_open()? → skip if yes
  │           - place_long_order()   → submit bracket order
  │           - record_trade()       → update DailyRiskState

[get_top_signals() — signals/reader.py]
  │
  ├─ A. gate_market_conditions() → global_kill_switch check
  │
  ├─ B. for each symbol in universe[:max_symbols]:
  │      1. fetch_yahoo_snapshot()  → price, volume, ATR, market_cap
  │      2. price range pre-filter  → reject if < $5 or > $1000
  │      3. get_symbol_features()   → Yahoo + Quiver numeric features
  │      4. _quiver_fast_lane_summary():
  │           - insider_buys >= 2 in 7d?  → FAST LANE
  │           - gov_contract >= $1M 30d?  → FAST LANE
  │           - patent_momentum >= 1.0?   → FAST LANE
  │      5. Yahoo Gate (strict OR relaxed if fast lane):
  │           STRICT:  market_cap >= $1B, volume >= 250k, ATR% <= 6%
  │           RELAXED: market_cap >= $300M, volume >= 50k, ATR% <= 12%
  │      6. Quiver Gate (gate_quiver_minimum()):
  │           - If fast lane → gate bypassed (auto-pass)
  │           - Else: check insider_buy_min_count_lookback=1
  │                   AND active_types >= 2
  │      7. Score: Σ(feature × weight) capped per feature
  │      8. Append to candidates if all gates pass
  │
  ├─ C. rank candidates by (score_total, quiver_strength, volume_7d) DESC
  │
  └─ D. risk_manager.plan_trades(ranked) → approved plans

[risk_manager.plan_trades()]
  ├─ _get_account_snapshot() → equity, cash, positions, orders
  ├─ for each candidate:
  │    _compute_order_plan() → qty, notional, stop, take_profit
  │    check_risk_limits()   → daily_spend, position_count, exposure, cooldown
  └─ return approved, rejections
```

---

## 5. Quiver Quant PRO — Endpoints & Features

### Endpoints Used (all via `quiver_ingest.py`)

| Endpoint | URL | Cache Strategy | Feature Extracted |
|----------|-----|----------------|-------------------|
| Live Insiders | `/beta/live/insiders` | Heavy (24h) | buy_count, sell_count |
| Live Gov Contracts | `/beta/live/govcontracts` | Heavy (24h) | total_amount, count |
| Live House Trading | `/beta/live/housetrading` | Heavy (24h) | house_purchase_count |
| Live Twitter | `/beta/live/twitter` | Heavy (24h) | latest_followers |
| Live App Ratings | `/beta/live/appratings` | Heavy (24h) | latest_rating, count |
| Live SEC 13F | `/beta/live/sec13f` | Heavy (24h) | sec13f_count |
| Live SEC 13F Changes | `/beta/live/sec13fchanges` | Heavy (24h) | sec13f_change_latest_pct |
| Live Patent Momentum | `/beta/live/patentmomentum` | Per-request | patent_momentum_latest |
| Historical WSB | `/beta/historical/wallstreetbets/{ticker}` | Per-request | wsb_recent_max_mentions |

### Field Mapping (API → Feature)

```python
# Insider trades (TransactionCode field from SEC Form 4):
#   "P" = open market purchase  → quiver_insider_buy_count
#   "S" = open market sale      → quiver_insider_sell_count

# Gov contracts:
#   Amount (USD)  → quiver_gov_contract_total_amount
#   count         → quiver_gov_contract_count

# Patent momentum:
#   momentum (float) → quiver_patent_momentum_latest
#   (proprietary metric: recent perf of company's tech peers)

# WSB (Wall Street Bets):
#   Mentions (integer per day) → quiver_wsb_recent_max_mentions (max of last 5 records)

# SEC 13F:
#   count of holdings entries → quiver_sec13f_count
#   Change_Pct (float)        → quiver_sec13f_change_latest_pct

# Twitter:
#   Followers (integer) → quiver_twitter_latest_followers

# App ratings:
#   Rating (0-5 float)  → quiver_app_rating_latest
#   Count (integer)     → quiver_app_rating_latest_count
```

### Data Freshness Filtering

All features filtered by `freshness_days_quiver: 7` (default 7 days).
**Exception**: Gov contracts are reported quarterly — a 7-day freshness window discards most valid contracts. The `gov_contract_min_total_amount: 0` threshold disables the gate check, but the freshness filter in `quiver_utils._gov_contract_features()` still drops quarterly data. Consider a separate `freshness_days_gov_contracts` config key.

---

## 6. Scoring System

### Feature Weights (`QUIVER_FEATURE_WEIGHTS` in `signals/reader.py`)

```python
QUIVER_FEATURE_WEIGHTS = {
    "quiver_insider_buy_count":         1.0,
    "quiver_insider_sell_count":       -1.0,
    "quiver_gov_contract_total_amount": 0.000001,
    "quiver_gov_contract_count":        0.5,
    "quiver_patent_momentum_latest":    1.0,
    "quiver_wsb_recent_max_mentions":   0.05,
    "quiver_sec13f_count":              0.2,
    "quiver_sec13f_change_latest_pct":  0.1,
    "quiver_twitter_latest_followers":  0.00005,
    "quiver_app_rating_latest":         0.2,
    "quiver_app_rating_latest_count":   0.02,
}
```

### Feature Caps (`_FEATURE_CAPS` in `signals/reader.py`)

```python
_FEATURE_CAPS = {
    "quiver_gov_contract_total_amount": 200_000_000,   # $200M cap → max 200 pts
    "quiver_wsb_recent_max_mentions":   500,            # max 25 pts
    "quiver_insider_buy_count":         5,              # max 5 pts
    "quiver_gov_contract_count":        5,              # max 2.5 pts
    "quiver_sec13f_change_latest_pct":  20,             # max 2 pts
    "quiver_patent_momentum_latest":    5,              # max 5 pts
    # ⚠️ quiver_twitter_latest_followers has NO cap → can generate 1000s of points
}
```

### Score Formula

```
total_score = Σ min(feature_value, cap) × weight
```

Score is used for **ranking** only, not for binary pass/fail (no `approval_threshold` set by default).

---

## 7. Gates

### A. Market Gate (`gate_market_conditions`)
- Checks `global_kill_switch` in policy.yaml
- If true → entire scan aborts immediately

### B. Yahoo Gate (price pre-filter → then full)
**Step 1** — Price range (always, before Quiver fetch):
- `current_price` < $5 or > $1000 → reject

**Step 2** — Full gate (after Quiver features, mode depends on fast lane):

| | Strict (default) | Relaxed (fast lane) |
|-|-----------------|---------------------|
| market_cap | ≥ $1B | ≥ $300M |
| avg_volume_7d | ≥ 250k | ≥ 50k |
| max_atr_pct | ≤ 6% | ≤ 12% |
| require_trend | false | false |

With `STRICT_GATES=true` env var: forces `min_avg_volume_7d=1M` and `require_trend_positive=True`.

### C. Quiver Fast Lane (`_quiver_fast_lane_summary`)
Triggered when **any** of:
- `insider_buy_count` ≥ `insider_buy_strong_min_count_7d` (default 2)
- `gov_contract_total_amount` ≥ `gov_contract_strong_min_total_30d` (default $1M)
- `patent_momentum_latest` ≥ `patent_momentum_min_strong` (default 1.0)

Effect: relaxed Yahoo gate + automatic Quiver gate pass.

### D. Quiver Minimum Gate (`gate_quiver_minimum`)
**Auto-disabled** when: `ENABLE_QUIVER=false` OR `enabled=false` OR all thresholds = 0.

With current policy (`insider_buy_min_count_lookback: 1`, all others = 0):
1. `checks` = [`insider_buy_count >= 1`]
2. If `not any(checks)` → reject `quiver_min_signal`
3. Count `active_types` (features with value > 0 across: insider buys, gov contracts, patent, sec13f, wsb)
4. If `active_types < 2` → reject `quiver_min_types`
   ⚠️ This is **hardcoded** at 2. See §9 Known Issues.

---

## 8. Risk Manager

### Position Sizing Formula (`risk_manager._compute_order_plan`)

```
stop_distance = max(ATR × atr_k,  price × min_stop_pct)
             = max(ATR × 2.0,     price × 5%)

risk_budget   = equity × max_symbol_risk_pct   (1% of equity)
risk_qty      = floor(risk_budget / stop_distance)
max_affordable= floor(size_usd / price)
qty           = min(risk_qty, max_affordable)

size_usd      = min(max_position_size_usd, remaining_budget, cash_available)
              = min($500, remaining_daily, cash - buffer)
```

### Risk Checks (`check_risk_limits`)

| Check | Rejection reason |
|-------|-----------------|
| `spent_today >= daily_max_spend_usd` | `daily_spend_exceeded` |
| `new_positions_today >= daily_max_new_positions` | `daily_positions_exceeded` |
| `len(positions) >= max_total_open_positions` | `max_open_positions` |
| `equity <= 0` | `invalid_equity` |
| `(cash/equity) < cash_buffer_pct` | `cash_buffer` |
| Total exposure ≥ 60% equity | `max_exposure` |
| Symbol exposure ≥ 12% equity | `symbol_exposure` |
| Position already open for symbol | `position_open` |
| Order pending for symbol | `order_pending` |
| Already traded symbol today | `symbol_traded_today` |
| Last trade < 5 days ago | `symbol_cooldown` |
| Planned spend ≤ 0 | `invalid_plan_spend` |

### DailyRiskState Persistence
Stored in `data/risk_state.json`. Resets counters automatically on new trading day (NYSE timezone).

---

## 9. Bracket Orders & Execution

### Order Flow (`executor.place_long_order`)

```python
# Guards (all must pass):
is_safeguards_active()   # TTL check — ⚠️ see Known Issues
use_bracket == True      # Only bracket orders allowed
qty > 0 and symbol set   # Basic validation

# Prices:
stop_price  = entry - max(ATR × 2.0, entry × 5%)  [rounded DOWN to tick]
take_profit = entry + ATR × 3.0                    [rounded UP to tick]
stop_limit  = stop_price × (1 - 0.002)             [2bp buffer]

# Submission:
order_class = "bracket"
type        = "market"
time_in_force = "day"
```

### Bracket Price Validation
- `stop_price > 0` and `take_profit > 0`
- `stop_price < entry_price`
- `take_profit > entry_price`
- `rr_ratio >= min_rr_ratio` (1.2)

---

## 10. Position Protector / Safeguards

Runs every 60 seconds via `tick_protect_positions()` → `run_safeguards()`.

### Break-Even Logic
```
R = (last_price - entry) / (entry - initial_stop)
If R >= break_even_R (1.0):
    new_stop = entry × (1 + break_even_buffer_pct)  → entry + 0.1%
```
Only triggers if `new_stop > initial_stop` (never moves stop down).

### Trailing Stop Activation
```
threshold = entry + 1.5 × (entry - initial_stop)   # 1.5R gain
If last_price >= threshold AND no trailing order yet:
    submit trailing_stop order (trail_percent=2%)
    cancel existing stop order
```

### Safeguards TTL Guard
`is_safeguards_active()` checks:
```python
expires_at = started_at_utc + timedelta(days=ttl_days)
return datetime.now(utc) < expires_at
```
If expired: NO new orders can be placed AND NO existing position protection runs.

---

## 11. Running the Bot

### Entry Points

```bash
# Minimal loop (production):
python main.py

# FastAPI server (scheduler in background thread):
python start.py

# Dry run (log orders but don't submit):
DRY_RUN=true python main.py
```

### Tests

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

### First-Run Setup

If `data/symbols.csv` is missing, the bot auto-generates it from Alpaca's asset list.
To manually regenerate:
```bash
python -c "from utils.generate_symbols_csv import generate_symbols_csv; generate_symbols_csv()"
```

### Caching

- **In-memory**: `utils/cache.py` — TTL-based dict, fast, resets on restart
- **Persistent**: `utils/persistent_cache.py` — file-backed, survives restarts
- Heavy Quiver endpoints (insiders, govcontracts, housetrading, twitter) → 24h cache
- Per-symbol Quiver features → 10min cache (key: `Q_SIG:{TICKER}`)
- Yahoo data → 5min in-memory cache per symbol

---

## 12. Logging System

All events logged via `utils/logger.log_event(msg, event=)`.

### Event Prefixes

| Prefix | When |
|--------|------|
| `SCAN` | Universe scan loop |
| `GATE` | Market gate checks |
| `TRACE` | Full per-symbol decision trace (JSON) |
| `ORDER` | Order submission attempts |
| `ORDER_SUBMIT` | Detailed bracket order params |
| `RISK` | Risk manager decisions |
| `RISK_PROTECT` | Position protector actions |
| `PROTECT` | Protector loop errors |
| `APPROVAL` | Post-signal approval/rejection |
| `CACHE` | Cache hits/misses/suppressions |

TRACE events contain full JSON decision_trace per symbol:
```json
{
  "symbol": "AAPL",
  "yahoo_prefilter_pass": true,
  "yahoo_mode_used": "strict_default",
  "gates_passed": {"market": true, "yahoo": true, "quiver": false},
  "quiver_gate_reasons": ["quiver_min_types"],
  "quiver_signal_summary": {"insider_buys_7d": 1, "strong_signal_bool": false},
  "final_decision": "REJECT"
}
```

---

## 13. Ticker Mapping

`data/symbols.csv` has three columns:

| Column | Description |
|--------|-------------|
| `Symbol` | Alpaca canonical ticker |
| `YahooSymbol` | Yahoo Finance ticker (may differ: e.g. BRK.B → BRK-B) |
| `QuiverSymbol` | Quiver Quant ticker (usually same as canonical) |

If >60% of symbols fail Yahoo fetch → scan aborts (`mapping_failure_pct_block`).

---

## 14. Key Invariants (NEVER VIOLATE)

1. **Never move a stop downward.** All stop updates are `max(old_stop, new_stop)`.
2. **Only bracket orders.** Unprotected market orders are rejected by `executor.py`.
3. **Safeguards must be active.** `is_safeguards_active()` gates both order placement AND position protection.
4. **All operations must be idempotent.** The protector loop runs every 60s and must be safe to re-run.
5. **Risk manager has the final word.** Even if signals pass all gates, risk limits can reject.
6. **DRY_RUN is honored everywhere.** Check `config.DRY_RUN` before any broker call.
7. **Logs are the audit trail.** Every decision (approval or rejection, with reasons) must be logged.
8. **Never decrease position count by re-entering a losing trade.** `symbol_cooldown_days: 5` enforces this.

---

## 15. Known Issues & Required Fixes

### 🔴 CRITICAL — System cannot place any orders

#### Issue 1: Safeguards TTL Expired
- **File**: `config/policy.yaml`
- **Problem**: `started_at_utc: "2026-01-29T00:00:00Z"` + `ttl_days: 7` = expired 2026-02-05.
  `is_safeguards_active()` returns `False` → `place_long_order()` rejects all orders with `safeguards_inactive`. Risk manager also returns `None, "safeguards_inactive"` for all bracket orders.
- **Fix**: Update `started_at_utc` to today's date, or set `ttl_days: 365` for a yearly cycle.

#### Issue 2: `active_types < 2` Hardcoded (near-universal Quiver gate rejection)
- **File**: `signals/reader.py:285`
- **Problem**: Even when `insider_buy_min_count_lookback: 1` passes (insider_buys ≥ 1), the symbol is still rejected unless ≥ 2 types of Quiver signals are active simultaneously. Most stocks on any given day have only 1 type of signal active.
- **Fix**: Add `min_active_signal_types: 1` to `policy.yaml → quiver_gate` and read it in `gate_quiver_minimum()` instead of hardcoded `2`.

---

### 🟠 HIGH — Score distortion / signal quality

#### Issue 3: `quiver_twitter_latest_followers` has no cap in `_FEATURE_CAPS`
- **File**: `signals/reader.py`
- **Problem**: Weight is `0.00005`, no cap. A company with 20M followers scores +1000 pts from Twitter alone, drowning all other signals.
- **Fix**: Add `"quiver_twitter_latest_followers": 10_000_000` to `_FEATURE_CAPS`.

#### Issue 4: `volume_7d_avg` is actually a 90-day average
- **File**: `signals/scoring.py:59`
- **Problem**: `hist["Volume"].mean()` uses the full 90-day history window, not 7 days.
- **Fix**: `hist["Volume"].tail(7).mean()`

#### Issue 5: Gov contracts freshness too short for quarterly data
- **File**: `signals/quiver_utils.py:_gov_contract_features`
- **Problem**: Gov contracts are reported quarterly, but `freshness_days_quiver: 7` discards any contract older than 7 days. In practice, this means gov contract features are almost always 0.
- **Fix**: Add separate `freshness_days_gov_contracts: 90` to policy and use it in `_gov_contract_features`.

---

### 🟡 MEDIUM — Performance / caching

#### Issue 6: `_sec13f_features` and `_sec13f_change_features` skip cache
- **File**: `signals/quiver_utils.py:152,170`
- **Problem**: Calls `fetch_live_sec13f()` and `fetch_live_sec13fchanges()` (uncached). Should call `fetch_live_sec13f_cached()` and `fetch_live_sec13fchanges_cached()`.

#### Issue 7: `_patent_momentum_features` skips cache
- **File**: `signals/quiver_utils.py:115`
- **Problem**: Calls `fetch_live_patentmomentum()` (uncached per-request). Should use a cached version via `_cached_heavy_endpoint`.

#### Issue 8: `_app_ratings_features` skips cache
- **File**: `signals/quiver_utils.py:227`
- **Problem**: Calls `fetch_live_appratings()` (uncached). Should use `fetch_live_appratings_cached()`.

#### Issue 9: WSB uses per-symbol historical endpoint (N API calls per scan)
- **File**: `signals/quiver_utils.py:133`
- **Problem**: `fetch_historical_wallstreetbets(symbol)` makes one API call per symbol. With 30 symbols in the scan → 30 requests per cycle. The bulk `live/wallstreetbets` endpoint exists and would be 1 call cached.

---

### 🔵 LOW — Minor improvements

#### Issue 10: `market_cap` can be `None` from Yahoo Finance
- **File**: `signals/reader.py:199`
- **Problem**: `(market_cap or 0) < min_market_cap` → when `market_cap is None`, evaluates as `0 < $1B` → rejection. Some valid stocks have no marketCap in YF info.

#### Issue 11: `client_order_id` collision risk
- **File**: `core/executor.py:100`
- **Problem**: `f"LONG.{symbol}.{int(price * 100)}"` — if same symbol is traded at same price twice in a session, duplicate client_order_id causes Alpaca rejection.
- **Fix**: Add timestamp: `f"LONG.{symbol}.{int(price*100)}.{int(time.time())}"`

---

## 16. Development Conventions

### Adding a New Quiver Feature

1. Add fetch function in `quiver_ingest.py` (use `_cached_heavy_endpoint` for bulk endpoints)
2. Add extractor function in `quiver_utils.py` (follow `_xxx_features()` pattern returning `(dict, ages)`)
3. Call it in `get_quiver_features()` and include in return dict
4. Add weight in `QUIVER_FEATURE_WEIGHTS` in `reader.py`
5. Add cap in `_FEATURE_CAPS` if the raw value can be very large
6. If it should trigger fast lane, add to `_quiver_fast_lane_summary()`
7. If it should count as an "active type", add to `gate_quiver_minimum()` counter

### Adding a New Threshold

**Always add to `policy.yaml`** with a sensible default. Read via:
```python
cfg = (getattr(config, "_policy", {}) or {}).get("section_name", {}) or {}
value = float(cfg.get("key_name", default_value))
```

### Error Handling Pattern
- Quiver API errors → `QuiverRateLimitError` or `QuiverTemporaryError` → endpoint suppressed for TTL duration
- Yahoo errors → `YFPricesMissingError` or `SkipSymbol` → symbol skipped for current cycle
- Broker errors → logged, order marked as failed, next symbol tried

### Logging Pattern
```python
log_event(f"GATE {symbol}: rejected reason=market_cap_low value={market_cap}", event="GATE")
log_event(f"ORDER {symbol}: submitted qty={qty} price={price:.2f}", event="ORDER")
```
Always include `reason=` tag for any rejection.
