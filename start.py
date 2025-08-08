"""Application entry point for Render background worker.

Launches scheduler tasks via ``launch_all`` then runs Uvicorn. If the server
crashes unexpectedly it is automatically restarted. Normal shutdown signals are
handled by Uvicorn so the process exits cleanly when requested.
"""

from __future__ import annotations

import os
import time
import uvicorn

from main import app, launch_all


def run_server() -> None:
    """Run the Uvicorn server respecting the ``PORT`` environment variable."""
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    launch_all()  # Start schedulers and heartbeat

    while True:
        try:
            run_server()
        except Exception:
            print("\u26a0\ufe0f  Server crashed, restarting...", flush=True)
            time.sleep(1)
        else:
            print("\U0001F44B Server stopped", flush=True)
            break
