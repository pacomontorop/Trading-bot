#import os
import time
import requests
from dotenv import load_dotenv
from utils.logger import log_event

load_dotenv()

QUIVER_API_KEY = os.getenv("QUIVER_API_KEY")
QUIVER_BASE_URL = "https://api.quiverquant.com"
HEADERS = {"x-api-key": QUIVER_API_KEY}

QUIVER_SIGNAL_WEIGHTS = {
    "insider_buy_more_than_sell": 4,
    "has_gov_contract": 3,
    "positive_patent_momentum": 2,
    "trending_wsb": 1,
    "bullish_etf_flow": 2,
    "has_recent_sec13f_activity": 2,
    "has_recent_sec13f_changes": 2,
    "has_recent_dark_pool_activity": 3,
    "is_high_political_beta": 1,
    "is_trending_on_twitter": 1,
    "has_positive_app_ratings": 2
}

QUIVER_APPROVAL_THRESHOLD = 6

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

def get_quiver_signals(symbol):
    signals = {
        "insider_buy_more_than_sell": is_strong_buy_by_insider_trading(symbol),
        "has_gov_contract": has_recent_gov_contract(symbol),
        "positive_patent_momentum": has_positive_patent_momentum(symbol),
        "trending_wsb": is_trending_in_wsb(symbol),
        "bullish_etf_flow": has_bullish_etf_flow(symbol)
    }
    return signals

def score_quiver_signals(signals):
    return sum(QUIVER_SIGNAL_WEIGHTS.get(k, 0) for k, v in signals.items() if v)

def is_approved_by_quiver(symbol):
    try:
        signals = get_quiver_signals(symbol)
        score = score_quiver_signals(signals)
        active = [k for k, v in signals.items() if v]
        if score >= QUIVER_APPROVAL_THRESHOLD:
            log_event(f"✅ {symbol} aprobado por Quiver con score {score}. Señales activas: {', '.join(active)}")
            return True
        print(f"⛔ {symbol} no aprobado por Quiver. Score: {score}. Activas: {', '.join(active)}")
    except Exception as e:
        print(f"⚠️ Error evaluando Quiver para {symbol}: {e}")
    return False

# Funciones auxiliares individuales

def is_strong_buy_by_insider_trading(symbol):
    try:
        url = f"{QUIVER_BASE_URL}/live/insidertrading/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list):
            total_buy = sum(1 for tx in data if tx.get("Transaction") == "Purchase")
            total_sell = sum(1 for tx in data if tx.get("Transaction") == "Sale")
            return total_buy > total_sell
        else:
            print(f"⚠️ Datos inválidos de insider trading para {symbol}")
    except Exception as e:
        print(f"⚠️ Error en is_strong_buy_by_insider_trading para {symbol}: {e}")
    return False

def has_recent_gov_contract(symbol):
    try:
        url = f"{QUIVER_BASE_URL}/live/govcontracts/{symbol}"
        data = safe_quiver_request(url)
        return isinstance(data, list) and len(data) > 0
    except Exception as e:
        print(f"⚠️ Error en has_recent_gov_contract para {symbol}: {e}")
    return False

def has_positive_patent_momentum(symbol):
    try:
        url = f"{QUIVER_BASE_URL}/live/patentmomentum/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list) and len(data) > 0:
            momentum = data[0].get("Momentum", 0)
            return momentum > 0
        else:
            print(f"⚠️ Sin datos válidos de momentum para {symbol}")
    except Exception as e:
        print(f"⚠️ Error en has_positive_patent_momentum para {symbol}: {e}")
    return False

def is_trending_in_wsb(symbol):
    try:
        url = f"{QUIVER_BASE_URL}/live/wsb/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list) and len(data) > 0:
            mentions = data[0].get("Mentions", 0)
            return mentions > 10
    except Exception as e:
        print(f"⚠️ Error en is_trending_in_wsb para {symbol}: {e}")
    return False

def has_bullish_etf_flow(symbol):
    try:
        url = f"{QUIVER_BASE_URL}/live/etf/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list) and len(data) > 0:
            net_flow = data[0].get("NetFlow", 0)
            return net_flow > 0
        else:
            print(f"⚠️ Sin datos válidos de flujo ETF para {symbol}")
    except Exception as e:
        print(f"⚠️ Error en has_bullish_etf_flow para {symbol}: {e}")
    return False
