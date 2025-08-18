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

# Flag para evitar iniciar los schedulers múltiples veces
schedulers_started = threading.Event()

@app.get("/ping")
def ping():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


def heartbeat():
    while True:
        print(f"💓 Alive at {datetime.utcnow().isoformat()} UTC", flush=True)
        time.sleep(300)  # Cada 5 minutos para evitar inactividad prolongada


def start_schedulers_once():
    if not schedulers_started.is_set():
        start_schedulers()
        schedulers_started.set()


def launch_all():
    print("🟢 Lanzando schedulers...", flush=True)
    start_schedulers_once()
    start_metrics_server()
    threading.Thread(target=heartbeat, daemon=True).start()


def launch_crypto_only():
    print("🪙 Lanzando solo el worker de cripto...", flush=True)
    threading.Thread(target=crypto_worker, daemon=True).start()
    start_metrics_server()
    threading.Thread(target=heartbeat, daemon=True).start()


def await_market_open():
    """Espera a que abra el mercado para iniciar los schedulers de acciones."""
    while not schedulers_started.is_set():
        if is_market_open():
            print("🔔 Mercado abierto. Iniciando schedulers de acciones...", flush=True)
            start_schedulers_once()
            break
        time.sleep(60)


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
    # Siempre lanzar un hilo que vigile la apertura del mercado
    threading.Thread(target=await_market_open, daemon=True).start()
