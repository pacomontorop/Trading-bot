#main.py

from fastapi import FastAPI
import threading
import time
from core.scheduler import start_schedulers
from utils.monitoring import start_metrics_server
from datetime import datetime
from broker.alpaca import is_market_open

app = FastAPI()

@app.get("/ping")
def ping():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


def heartbeat():
    while True:
        print(f"💓 Alive at {datetime.utcnow().isoformat()} UTC", flush=True)
        time.sleep(300)  # Cada 5 minutos para evitar inactividad prolongada


def launch_all():
    if not is_market_open():
        print("⛔ Mercado cerrado. Schedulers no iniciados.", flush=True)
        return
    print("🟢 Lanzando schedulers...", flush=True)
    start_schedulers()
    start_metrics_server()
    threading.Thread(target=heartbeat, daemon=True).start()
