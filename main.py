#main.py

from fastapi import FastAPI
import threading
import time
from datetime import datetime

from core.scheduler import start_equity_scheduler
from utils.monitoring import start_metrics_server

app = FastAPI()

# Flags y referencias de hilos
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
        start_equity_scheduler()
        schedulers_started.set()


@app.on_event("startup")
def on_startup():
    start_metrics_server()
    threading.Thread(target=heartbeat, daemon=True).start()
    start_schedulers_once()
