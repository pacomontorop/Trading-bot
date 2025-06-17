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
