from __future__ import annotations

import os
import sys
import threading
import warnings

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas")
warnings.filterwarnings("ignore", message="Timestamp.utcnow")

from fastapi import FastAPI

from core.scheduler import equity_scheduler_loop

# This bot is stateful (single scheduler loop, shared rotation state, on-disk
# risk files).  Multiple uvicorn workers would each start their own scheduler,
# causing duplicate scans and duplicate orders.  Refuse to start if misconfigured.
if int(os.environ.get("WEB_CONCURRENCY", "1")) > 1:
    sys.stderr.write(
        "FATAL: WEB_CONCURRENCY > 1 detected. "
        "This bot must run with a single worker (WEB_CONCURRENCY=1). "
        "Multiple workers cause duplicate scans and duplicate orders.\n"
    )
    sys.exit(1)

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
