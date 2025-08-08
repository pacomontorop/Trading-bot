"""Robust entry point for the trading bot service.

This script launches all background schedulers and starts the FastAPI server
using Uvicorn. It also implements basic auto-restart and graceful shutdown
to avoid unexpected termination in environments like Render.
"""

import signal
import time

import uvicorn

from main import app, launch_all


shutdown = False
server = None


def handle_exit(sig, frame):
    """Handle termination signals to stop the server gracefully."""
    global shutdown, server
    print(f"⚠️ Señal {sig} recibida. Deteniendo servidor...", flush=True)
    shutdown = True
    if server:
        server.should_exit = True


def run_server():
    """Run Uvicorn without its own signal handlers so we control restarts."""
    global server
    config = uvicorn.Config(app, host="0.0.0.0", port=8000)
    server = uvicorn.Server(config)
    server.install_signal_handlers = False
    server.run()


if __name__ == "__main__":
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handle_exit)

    launch_all()  # Lanza los schedulers + heartbeat

    while not shutdown:
        try:
            run_server()
        except Exception as exc:
            print(f"❌ Server crashed: {exc}", flush=True)

        if not shutdown:
            print("🔁 Reiniciando servidor en 5s...", flush=True)
            time.sleep(5)

