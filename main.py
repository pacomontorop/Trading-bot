#main.py

from fastapi import FastAPI
import threading
import time
from datetime import datetime

from broker.alpaca import is_market_open
from core.scheduler import start_schedulers
from core.crypto_worker import crypto_worker
from utils.monitoring import start_metrics_server

app = FastAPI()

@app.get("/ping")
def ping():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


def heartbeat():
    while True:
        print(f"💓 Alive at {datetime.utcnow().isoformat()} UTC", flush=True)
        time.sleep(300)  # Cada 5 minutos para evitar inactividad prolongada


def launch_all():
    print("🟢 Lanzando schedulers...", flush=True)
    start_schedulers()
    start_metrics_server()
    threading.Thread(target=heartbeat, daemon=True).start()


def launch_crypto_only():
    print("🪙 Lanzando solo el worker de cripto...", flush=True)
    threading.Thread(target=crypto_worker, daemon=True).start()
    start_metrics_server()
    threading.Thread(target=heartbeat, daemon=True).start()


@app.on_event("startup")
def on_startup():
    if is_market_open():
        launch_all()
    else:
        print(
            "⛔ Mercado cerrado. Solo se iniciará el worker de cripto.",
            flush=True,
        )
        launch_crypto_only()
