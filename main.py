pip install fastapi uvicorn

from fastapi import FastAPI
import threading
import time
from core.scheduler import start_schedulers
from datetime import datetime

app = FastAPI()

@app.get("/ping")
def ping():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


def heartbeat():
    while True:
        print(f"💓 Alive at {datetime.utcnow().isoformat()} UTC", flush=True)
        time.sleep(900)  # Cada 15 minutos


def launch_all():
    print("🟢 Lanzando schedulers...", flush=True)
    start_schedulers()
    threading.Thread(target=heartbeat, daemon=True).start()


if __name__ == "__main__":
    launch_all()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
