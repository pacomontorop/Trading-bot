import os
import time
import requests
from dotenv import load_dotenv
from utils.logger import log_event

load_dotenv()

QUIVER_API_KEY = os.getenv("QUIVER_API_KEY")
QUIVER_BASE_URL = "https://api.quiverquant.com/beta"
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
    return {
        "insider_buy_more_than_sell": get_insider_signal(symbol),
        "has_gov_contract": get_gov_contract_signal(symbol),
        "positive_patent_momentum": get_patent_momentum_signal(symbol),
        "trending_wsb": get_wsb_signal(symbol),
        "bullish_etf_flow": get_etf_flow_signal(symbol)
    }
    
def get_insider_signal(symbol):
    url = f"{QUIVER_BASE_URL}/live/insiders"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    symbol_data = [tx for tx in data if tx.get("Ticker") == symbol.upper()]
    buys = sum(1 for tx in symbol_data if tx.get("TransactionCode") == "P")
    sells = sum(1 for tx in symbol_data if tx.get("TransactionCode") == "S")
    return buys > sells


def get_gov_contract_signal(symbol):
    url = f"{QUIVER_BASE_URL}/live/govcontracts"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() for tx in data)

def get_patent_momentum_signal(symbol):
    url = f"{QUIVER_BASE_URL}/live/patentmomentum"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    matches = [tx for tx in data if tx.get("ticker") == symbol.upper()]
    return any(tx.get("momentum", 0) > 0 for tx in matches)
    
def get_wsb_signal(symbol):
    url = f"{QUIVER_BASE_URL}/historical/wallstreetbets/{symbol.upper()}"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    recent_mentions = [tx for tx in data if tx.get("Mentions", 0) > 10]
    return len(recent_mentions) > 0

def get_etf_flow_signal(symbol):
    url = f"{QUIVER_BASE_URL}/live/etfholdings"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    matches = [tx for tx in data if tx.get("Holding Symbol") == symbol.upper()]
    return any(tx.get("Value ($)", 0) > 0 for tx in matches)


# --- Señales extendidas Tier 1 y 2 ---

def get_extended_quiver_signals(symbol):
    return {
        "has_recent_sec13f_activity": has_recent_sec13f_activity(symbol),
        "has_recent_sec13f_changes": has_recent_sec13f_changes(symbol),
        "is_high_political_beta": is_high_political_beta(symbol),
        "is_trending_on_twitter": is_trending_on_twitter(symbol),
        "has_positive_app_ratings": has_positive_app_ratings(symbol)
    }

def has_recent_sec13f_activity(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/sec13f")
    return isinstance(data, list) and any(tx.get("Ticker") == symbol.upper() for tx in data)

def has_recent_sec13f_changes(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/sec13fchanges")
    return isinstance(data, list) and any(tx.get("Ticker") == symbol.upper() for tx in data)

def is_high_political_beta(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/politicalbeta")
    if isinstance(data, list):
        matches = [tx for tx in data if tx.get("Ticker") == symbol.upper()]
        return any(tx.get("TrumpBeta", 0) > 0.25 for tx in matches)
    return False

def is_trending_on_twitter(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/twitter")
    if isinstance(data, list):
        matches = [tx for tx in data if tx.get("Ticker") == symbol.upper()]
        return any(tx.get("Followers", 0) > 0 for tx in matches)
    return False

def has_positive_app_ratings(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/appratings")
    if isinstance(data, list):
        matches = [tx for tx in data if tx.get("Ticker") == symbol.upper()]
        return any(tx.get("Rating", 0) >= 3.5 for tx in matches)
    return False
