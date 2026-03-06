"""Entry point for the minimal long-only trading loop."""

import warnings

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas")

from core.scheduler import equity_scheduler_loop


if __name__ == "__main__":
    equity_scheduler_loop()
