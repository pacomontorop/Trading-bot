"""Entry point for the minimal long-only trading loop."""

from core.scheduler import equity_scheduler_loop


if __name__ == "__main__":
    equity_scheduler_loop()
