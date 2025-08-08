"""Application entry point for Render background worker.

This script launches scheduler tasks via ``launch_all`` and then runs the
Uvicorn server. Custom signal handlers are registered so we can coordinate a
graceful shutdown and automatically restart the server if it exits
unexpectedly.
"""

from __future__ import annotations

import os
import signal
import time
import uvicorn

from main import app, launch_all


shutdown = False
_server: uvicorn.Server | None = None


def _handle_signal(signum, frame):
    """Mark the process for shutdown and ask the server to exit."""
    global shutdown, _server
    shutdown = True
    if _server is not None:
        _server.should_exit = True


def _run_server():
    """Run Uvicorn without its own signal handlers."""
    global _server
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port)
    _server = uvicorn.Server(config)
    # Disable Uvicorn's internal signal handlers; we control shutdown ourselves.
    _server.install_signal_handlers = lambda: None
    _server.run()
    _server = None


if __name__ == "__main__":
    # Register custom handlers before starting anything else.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    launch_all()  # Start schedulers and heartbeat

    while not shutdown:
        try:
            _run_server()
        except Exception:
            # Sleep briefly before attempting a restart after a crash.
            if not shutdown:
                time.sleep(1)
        # Restart server if it exited unexpectedly.
        if not shutdown:
            time.sleep(1)

