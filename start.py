from __future__ import annotations

import os
import sys
import threading
import warnings

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas")
warnings.filterwarnings("ignore", message="Timestamp.utcnow")
# Broad catch: Pandas4Warning (FutureWarning subclass) for utcnow deprecation
# issued deep inside yfinance — the module filter above doesn't reach it.
warnings.filterwarnings("ignore", message=".*utcnow.*")
warnings.filterwarnings("ignore", category=FutureWarning)

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

# 2026-09-11 — decisión de Paco: el bot de Render queda APAGADO por defecto. El sistema que
# opera es GitHub Actions (scripts/*.py + motor.yml). Este proceso sigue respondiendo al
# healthcheck para que Render no lo reinicie en bucle, pero NO arranca el scheduler (ni
# escaneo, ni órdenes, ni PROTECT). Para volver a encenderlo: variable RENDER_BOT_ENABLED=1.
BOT_ENABLED = os.environ.get("RENDER_BOT_ENABLED", "0") == "1"


def _start_scheduler() -> None:
    if not BOT_ENABLED:
        sys.stderr.write("Render bot DESACTIVADO (RENDER_BOT_ENABLED != 1): scheduler no arrancado.\n")
        try:
            from utils.telegram_alert import send_telegram_alert
            send_telegram_alert("⏸️ Bot de Render desactivado por código (RENDER_BOT_ENABLED≠1). "
                                "Opera solo el sistema de GitHub Actions.")
        except Exception:
            pass
        return
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
    return {"status": "ok", "bot": "enabled" if BOT_ENABLED else "disabled"}


if __name__ == "__main__":
    if BOT_ENABLED:
        equity_scheduler_loop()
    else:
        sys.stderr.write("Render bot DESACTIVADO (RENDER_BOT_ENABLED != 1).\n")
