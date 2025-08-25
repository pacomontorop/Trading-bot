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

# Flags y referencias de hilos
schedulers_started = threading.Event()
crypto_thread = None
crypto_stop_event = threading.Event()

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


def start_crypto_worker_thread():
    global crypto_thread, crypto_stop_event
    if crypto_thread is None or not crypto_thread.is_alive():
        crypto_stop_event.clear()
        crypto_thread = threading.Thread(
            target=crypto_worker, args=(crypto_stop_event,), daemon=True
        )
        crypto_thread.start()


def stop_crypto_worker_thread():
    global crypto_thread, crypto_stop_event
    if crypto_thread and crypto_thread.is_alive():
        crypto_stop_event.set()
        crypto_thread = None


def manage_crypto_worker():
    while True:
        if is_market_open():
            stop_crypto_worker_thread()
        else:
            start_crypto_worker_thread()
        time.sleep(60)


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
    start_metrics_server()
    threading.Thread(target=heartbeat, daemon=True).start()

    if is_market_open():
        print("🟢 Mercado abierto. Iniciando schedulers de acciones...", flush=True)
        start_schedulers_once()
    else:
        print("⛔ Mercado cerrado. Esperando apertura para acciones...", flush=True)

    threading.Thread(target=await_market_open, daemon=True).start()
    threading.Thread(target=manage_crypto_worker, daemon=True).start()
