from core.scheduler import start_schedulers
import time
import threading
from datetime import datetime


def heartbeat():
    while True:
        print(f"💓 Alive at {datetime.utcnow().isoformat()} UTC", flush=True)
        time.sleep(900)  # Cada 15 minutos

if __name__ == "__main__":
    print("🟢 Lanzando schedulers...", flush=True)
    start_schedulers()

    # 🫀 Hilo de latido para verificar que el proceso sigue vivo
    threading.Thread(target=heartbeat, daemon=True).start()

    # 🔁 Mantener vivo el proceso aunque todos los hilos sean daemon
    while True:
        time.sleep(3600)
