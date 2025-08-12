# start.py
import sys
import uvicorn
from broker.alpaca import is_market_open
from main import app, launch_all

# Evitar iniciar el sistema si el mercado está cerrado
if not is_market_open():
    print("⛔ Mercado cerrado. El sistema no se iniciará.", flush=True)
    sys.exit(0)

launch_all()  # Lanza los schedulers + heartbeat

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
