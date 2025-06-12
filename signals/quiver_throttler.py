# quiver_throttler.py

import time
import random
import threading

# Variables globales
REQUEST_LOCK = threading.Lock()
LAST_REQUEST_TIME = 0
RATE_LIMIT_DELAY = 1.2  # segundos entre peticiones, ajustable según tu plan


def throttled_request(request_func, *args, **kwargs):
    """
    Ejecuta una petición a la API de forma segura, asegurando que no se violen los límites de velocidad.
    Aplica un retardo entre llamadas sucesivas.
    """
    global LAST_REQUEST_TIME

    with REQUEST_LOCK:
        now = time.time()
        time_since_last = now - LAST_REQUEST_TIME

        if time_since_last < RATE_LIMIT_DELAY:
            sleep_time = RATE_LIMIT_DELAY - time_since_last + random.uniform(0, 0.5)
            time.sleep(sleep_time)

        try:
            result = request_func(*args, **kwargs)
            return result
        finally:
            LAST_REQUEST_TIME = time.time()
