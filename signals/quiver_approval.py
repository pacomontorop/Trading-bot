# quiver_approval.py
import os
import requests
from singals.approvals_fallback import is_approved_by_finnhub_and_alphavantage
from dotenv import load_dotenv
from utils.logger import log_event  
from signals.quiver_endpoints import get_extended_quiver_signals


load_dotenv()

QUIVER_API_KEY = os.getenv("QUIVER_API_KEY")
QUIVER_BASE_URL = "https://api.quiverquant.com"
HEADERS = {"x-api-key": QUIVER_API_KEY}

QUIVER_SIGNAL_WEIGHTS = {
    # Originales
    "insider_buy_more_than_sell": 4,
    "has_gov_contract": 3,
    "positive_patent_momentum": 2,
    "trending_wsb": 1,
    "bullish_etf_flow": 2,

    # Tier 1 y 2
    "has_recent_sec13f_activity": 2,
    "has_recent_sec13f_changes": 2,
    "has_recent_dark_pool_activity": 3,
    "is_high_political_beta": 1,
    "is_trending_on_twitter": 1,
    "has_positive_app_ratings": 2
}

QUIVER_APPROVAL_THRESHOLD = 6  # Cambia este número si quieres ser más estricto o permisivo


def get_quiver_signals(symbol):
    """
    Devuelve un diccionario con las señales de Quiver para un símbolo.
    Ideal para logging, scoring, backtesting y decisión desacoplada.
    """
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

def get_all_quiver_signals(symbol):
    basic_signals = get_quiver_signals(symbol)
    extended_signals = get_extended_quiver_signals(symbol)
    all_signals = {**basic_signals, **extended_signals}
    log_event(f"🧠 {symbol} señales Quiver combinadas: {all_signals}")
    return all_signals


def evaluate_quiver_signals(signals, symbol=""):
    score = score_quiver_signals(signals)
    active_signals = [k for k, v in signals.items() if v]

    if score >= QUIVER_APPROVAL_THRESHOLD:
        log_event(f"✅ {symbol} aprobado por Quiver con score {score}. Señales activas: {', '.join(active_signals)}")
        return True
    else:
        print(f"⛔ {symbol} no aprobado por Quiver. Score: {score}. Señales activas: {', '.join(active_signals)}")
        return False


def score_quiver_signals(signals):
    """
    Calcula un puntaje total según los pesos definidos.
    """
    score = 0
    for key, active in signals.items():
        if active:
            score += QUIVER_SIGNAL_WEIGHTS.get(key, 0)
    return score


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
        log_event(message)  # ← Añadido

    # Fallback
    if is_approved_by_finnhub_and_alphavantage(symbol):
        log_event(f"✅ {symbol} aprobado por fallback: Finnhub + Alpha.")
        return True

    print(f"⛔ {symbol} no aprobado por Quiver ni por Finnhub + Alpha.")
    return False

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


