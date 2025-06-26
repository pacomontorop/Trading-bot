import asyncio
import threading
import time

_loop = None
_thread = None

def _thread_entry():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

def start_quiver_loop():
    """Ensure the dedicated Quiver event loop is running in a background thread."""
    global _thread
    if _thread and _thread.is_alive():
        return _loop
    _thread = threading.Thread(target=_thread_entry, name="QuiverLoop", daemon=True)
    _thread.start()
    while _loop is None:
        time.sleep(0.01)
    return _loop


def run_in_quiver_loop(coro):
    """Run *coro* on the dedicated event loop and return its result."""
    loop = start_quiver_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()

