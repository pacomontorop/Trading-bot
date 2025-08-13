# start.py
import uvicorn
from broker.alpaca import is_market_open
from main import app, launch_all

# Evitar iniciar el sistema si el mercado está cerrado
if is_market_open():
    launch_all()  # Lanza los schedulers + heartbeat
else:
    print(
        "⛔ Mercado cerrado. La API permanecerá activa pero los schedulers no se iniciarán.",
        flush=True,
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
