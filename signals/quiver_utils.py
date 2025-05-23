import os
import time
import requests
from dotenv import load_dotenv
from utils.logger import log_event

load_dotenv()

QUIVER_API_KEY = os.getenv("QUIVER_API_KEY")
QUIVER_BASE_URL = "https://api.quiverquant.com"
HEADERS = {"x-api-key": QUIVER_API_KEY}

QUIVER_SIGNAL_WEIGHTS = {
    # Señales básicas
    "insider_buy_more_than_sell": 4,
    "has_gov_contract": 3,
    "positive_patent_momentum": 2,
    "trending_wsb": 1,
    "bullish_etf_flow": 2,

    # Señales extendidas Tier 1 y 2
    "has_recent_sec13f_activity": 2,
    "has_recent_sec13f_changes": 2,
    "has_recent_dark_pool_activity": 3,
    "is_high_political_beta": 1,
    "is_trending_on_twitter": 1,
    "has_positive_app_ratings": 2
}

QUIVER_APPROVAL_THRESHOLD = 6


# --- Función principal ---

def is_approved_by_quiver(symbol):
    try:
        signals = get_all_quiver_signals(symbol)
        if evaluate_quiver_signals(signals, symbol):
            return True
        else:
            print(f"⛔ {symbol} no aprobado por Quiver. Se evalúa con Finnhub + Alpha.")
    except Exception as e:
        message = f"⚠️ ERROR Quiver para {symbol}: {e}. Se recurre a fallback Finnhub+Alpha."
        print(message)
        log_event(message)

    # Fallback (debes importar en filters.py la función adecuada para que esto funcione)
    from signals.filters import is_approved_by_finnhub_and_alphavantage
    if is_approved_by_finnhub_and_alphavantage(symbol):
        log_event(f"✅ {symbol} aprobado por fallback: Finnhub + Alpha.")
        return True

    print(f"⛔ {symbol} no aprobado por Quiver ni por Finnhub + Alpha.")
    return False


# --- Evaluación y scoring ---

def get_all_quiver_signals(symbol):
    basic_signals = get_quiver_signals(symbol)
    extended_signals = get_extended_quiver_signals(symbol)
    all_signals = {**basic_signals, **extended_signals}
    log_event(f"🧠 {symbol} señales Quiver combinadas: {all_signals}")
    return all_signals

def evaluate_quiver_signals(signals, symbol=""):
    print(f"\n🧪 Evaluando señales Quiver para {symbol}...")

    # Mostrar todas las señales con su valor booleano
    for key, value in signals.items():
        status = "✅" if value else "❌"
        print(f"   {status} {key}: {value}")

    # Calcular puntuación
    score = score_quiver_signals(signals)
    active_signals = [k for k, v in signals.items() if v]

    # Mostrar puntuación total
    print(f"🧠 {symbol} → score total: {score} (umbral: {QUIVER_APPROVAL_THRESHOLD})")
    print(f"   Señales activas: {active_signals}")

    # Evaluación final
    if score >= QUIVER_APPROVAL_THRESHOLD:
        log_event(f"✅ {symbol} aprobado por Quiver con score {score}. Señales activas: {', '.join(active_signals)}")
        return True
    else:
        print(f"⛔ {symbol} no aprobado por Quiver. Score: {score}. Señales activas: {', '.join(active_signals)}")
        return False


def score_quiver_signals(signals):
    score = 0
    for key, active in signals.items():
        if active:
            score += QUIVER_SIGNAL_WEIGHTS.get(key, 0)
    return score


# --- Solicitudes robustas ---

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


# --- Señales básicas ---

def get_quiver_signals(symbol):
    signals = {
        "insider_buy_more_than_sell": False,
        "has_gov_contract": False,
        "positive_patent_momentum": False,
        "trending_wsb": False,
        "bullish_etf_flow": False
    }

    try:
        # Insider trading
        url = f"{QUIVER_BASE_URL}/live/insidertrading/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list):
            total_buy = sum(1 for tx in data if tx.get("Transaction") == "Purchase")
            total_sell = sum(1 for tx in data if tx.get("Transaction") == "Sale")
            signals["insider_buy_more_than_sell"] = total_buy > total_sell

        # Gov contracts
        url = f"{QUIVER_BASE_URL}/live/govcontracts/{symbol}"
        data = safe_quiver_request(url)
        signals["has_gov_contract"] = isinstance(data, list) and len(data) > 0

        # Patent momentum
        url = f"{QUIVER_BASE_URL}/live/patentmomentum/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list) and len(data) > 0:
            signals["positive_patent_momentum"] = data[0].get("Momentum", 0) > 0

        # WSB mentions
        url = f"{QUIVER_BASE_URL}/live/wsb/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list) and len(data) > 0:
            signals["trending_wsb"] = data[0].get("Mentions", 0) > 10

        # ETF flow
        url = f"{QUIVER_BASE_URL}/live/etf/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list) and len(data) > 0:
            signals["bullish_etf_flow"] = data[0].get("NetFlow", 0) > 0

    except Exception as e:
        print(f"⚠️ Error obteniendo señales Quiver para {symbol}: {e}")

    return signals


# --- Señales extendidas Tier 1 y 2 ---

def get_extended_quiver_signals(symbol):
    return {
        "has_recent_sec13f_activity": has_recent_sec13f_activity(symbol),
        "has_recent_sec13f_changes": has_recent_sec13f_changes(symbol),
        "has_recent_dark_pool_activity": has_recent_dark_pool_activity(symbol),
        "is_high_political_beta": is_high_political_beta(symbol),
        "is_trending_on_twitter": is_trending_on_twitter(symbol),
        "has_positive_app_ratings": has_positive_app_ratings(symbol)
    }

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
        return data[0].get("Beta", 0) > 1.0
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

