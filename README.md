# Trading Bot

Estructura modular del bot de trading.

## Tests

The unit tests are designed to run without network access.  External API calls
to QuiverQuant and `yfinance` are replaced with mocked responses so the suite
can be executed in isolated environments (e.g. CI) without real credentials.

## Configuration

Short-selling features can be toggled via the `ENABLE_SHORTS` environment
variable.  When unset or set to `false`, the scheduler will skip running the
short scan and only log long opportunities driven by Quiver signals.

Alpaca requests are made using a basic retry policy defined in
`broker/alpaca.py`. The underlying `requests` session retries failed calls up
to three times with an exponential backoff of three seconds. You can modify the
`Retry` settings in that module if a different strategy is required.

## Risk Management

The bot applies several layers of protection to limit losses:

* **Daily risk limit** – set `DAILY_RISK_LIMIT` to a negative dollar amount. The
  limit now considers both realized and unrealized PnL from open positions.
* **Virtual stops** – percentage and monetary thresholds can be customised via
  `TAKE_PROFIT_PCT`, `STOP_LOSS_PCT` and `MAX_LOSS_USD` environment variables
  (defaults: `5`, `-3` and `50`).
* **Monitoring frequency** – the position monitor and trailing-stop watchdog
  run more frequently; use `MONITOR_INTERVAL` and `TRAILING_WATCHDOG_INTERVAL`
  (seconds) to adjust the cadence.

## Examples

The `examples` folder contains small scripts illustrating how to use
`asyncio` with threads.  `examples/threaded_asyncio.py` demonstrates two
approaches for running coroutines from worker threads:

1. Scheduling the coroutine on the main loop using
   `asyncio.run_coroutine_threadsafe`.
2. Creating an independent event loop inside each thread.

## Utilities

`utils/log_summary.py` provides a small CLI to inspect `logs/events.log` for a given date.
It prints how many orders succeeded, failed, shorts were executed and any errors logged.

```bash
$ python utils/log_summary.py --date 2024-06-01
```

Without arguments it defaults to the current day:

```bash
$ python utils/log_summary.py
```

## FMP Backup

Set `FMP_API_KEY` to enable optional fallbacks to the Financial Modeling Prep API.
The bot will use FMP's stock screener when basic price data is missing and will
monitor analyst grade news to place small $10 trades when ratings switch between
buy/hold/sell.  Additional helpers expose company profiles, quotes and
fundamental metrics like financial ratios and key metrics for strategies that
need deeper fundamentals.
