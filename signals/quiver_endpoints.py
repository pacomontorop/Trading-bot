#quiver_endpoints.py

import os
import requests
import time
from dotenv import load_dotenv

load_dotenv()
QUIVER_API_KEY = os.getenv("QUIVER_API_KEY")
QUIVER_BASE_URL = "https://api.quiverquant.com/beta"
HEADERS = {"x-api-key": QUIVER_API_KEY}

def safe_quiver_request(url, retries=3, delay=2):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=5)
            if r.ok:
                return r.json()
        except Exception as e:
            print(f"⚠️ Error intentando {url} (intento {i+1}): {e}")
        time.sleep(delay)
    return None

# --- Nuevas señales para Tier 1 y Tier 2 ---

def has_recent_sec13f_activity(symbol):
    url = f"{QUIVER_BASE_URL}/live/sec13f/{symbol}"
    data = safe_quiver_request(url)
    return isinstance(data, list) and len(data) > 0

def has_recent_sec13f_changes(symbol):
    url = f"{QUIVER_BASE_URL}/live/sec13fchanges/{symbol}"
    data = safe_quiver_request(url)
    return isinstance(data, list) and len(data) > 0

def has_recent_dark_pool_activity(symbol):
    url = f"{QUIVER_BASE_URL}/live/offexchange/{symbol}"
    data = safe_quiver_request(url)
    return isinstance(data, list) and len(data) > 0

def is_high_political_beta(symbol):
    url = f"{QUIVER_BASE_URL}/live/politicalbeta/{symbol}"
    data = safe_quiver_request(url)
    if isinstance(data, list) and len(data) > 0:
        return data[0].get("Beta", 0) > 1.0  # umbral ajustable
    return False

def is_trending_on_twitter(symbol):
    url = f"{QUIVER_BASE_URL}/live/twitter/{symbol}"
    data = safe_quiver_request(url)
    if isinstance(data, list) and len(data) > 0:
        return data[0].get("Mentions", 0) > 10
    return False

def has_positive_app_ratings(symbol):
    url = f"{QUIVER_BASE_URL}/live/appratings/{symbol}"
    data = safe_quiver_request(url)
    if isinstance(data, list) and len(data) > 0:
        return data[0].get("AverageRating", 0) >= 4.0
    return False

# Fase 2: nueva función para incluir todas las señales Quiver en un único dict

def get_extended_quiver_signals(symbol):
    signals = {
        "has_recent_sec13f_activity": has_recent_sec13f_activity(symbol),
        "has_recent_sec13f_changes": has_recent_sec13f_changes(symbol),
        "has_recent_dark_pool_activity": has_recent_dark_pool_activity(symbol),
        "is_high_political_beta": is_high_political_beta(symbol),
        "is_trending_on_twitter": is_trending_on_twitter(symbol),
        "has_positive_app_ratings": has_positive_app_ratings(symbol)
    }
    return signals

