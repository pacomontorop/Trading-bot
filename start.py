from __future__ import annotations

import threading
import warnings

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas")

from fastapi import FastAPI

from core.scheduler import equity_scheduler_loop

app = FastAPI()


def _start_scheduler() -> None:
    thread = threading.Thread(
        target=equity_scheduler_loop,
        name="equity-scheduler",
        daemon=True,
    )
    thread.start()


@app.on_event("startup")
def start_scheduler_loop() -> None:
    _start_scheduler()


@app.get("/")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    equity_scheduler_loop()
