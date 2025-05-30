import os
import time
import requests
from dotenv import load_dotenv
from utils.logger import log_event
from datetime import datetime, timedelta

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
    "has_recent_house_purchase": 3,
    "is_trending_on_twitter": 1,
    "has_positive_app_ratings": 2
}

QUIVER_APPROVAL_THRESHOLD = 3


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
            r = requests.get(url, headers=HEADERS, timeout=15)
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

    cutoff_date = datetime.utcnow() - timedelta(days=7)

    symbol_data = [
        tx for tx in data
        if (tx.get("Ticker") or "").upper() == symbol.upper() and
           tx.get("TransactionCode") in {"P", "S"} and
           tx.get("Date")
    ]

    # Filtramos por fecha reciente
    recent_data = []
    for tx in symbol_data:
        try:
            tx_date = datetime.fromisoformat(tx["Date"].replace("Z", ""))
            if tx_date > cutoff_date:
                recent_data.append(tx)
        except Exception as e:
            continue

    buys = sum(1 for tx in recent_data if tx["TransactionCode"] == "P")
    sells = sum(1 for tx in recent_data if tx["TransactionCode"] == "S")

    return buys > sells



from datetime import datetime

def get_gov_contract_signal(symbol):
    url = f"{QUIVER_BASE_URL}/live/govcontracts"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False

    current_year = datetime.now().year
    current_month = datetime.now().month
    current_quarter = (current_month - 1) // 3 + 1  # Trimestre actual (1–4)

    symbol_contracts = [tx for tx in data if tx.get("Ticker") == symbol.upper()]

    for tx in symbol_contracts:
        try:
            amount = float(tx.get("Amount", "0").replace(",", "").replace("$", ""))
            year = int(tx.get("Year", 0))
            quarter = int(tx.get("Qtr", 0))
            if amount >= 100_000 and year >= current_year - 1:
                return True
        except:
            continue

    return False


def get_patent_momentum_signal(symbol):
    url = f"{QUIVER_BASE_URL}/live/patentmomentum"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False

    recent_entries = [tx for tx in data if tx.get("ticker") == symbol.upper()]
    
    # Solo cuenta como positivo si el valor momentum supera claramente 1.5
    return any(tx.get("momentum", 0) >= 1.2 for tx in recent_entries)


    
def get_wsb_signal(symbol):
    url = f"{QUIVER_BASE_URL}/historical/wallstreetbets/{symbol.upper()}"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False

    # Consideramos "recientes" los últimos 5 días
    recent_data = data[-5:] if len(data) >= 5 else data

    # Contamos los días con más de 10 menciones (indicativo de verdadero interés social)
    high_mention_days = [tx for tx in recent_data if tx.get("Mentions", 0) > 10]

    return len(high_mention_days) >= 1


def get_etf_flow_signal(symbol):
    url = f"{QUIVER_BASE_URL}/live/etfholdings?ticker={symbol.upper()}"  # ahora sí, con el ticker
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False

    total_exposure = sum(tx.get("Value ($)", 0) for tx in data)

    return total_exposure > 250_000





# --- Señales extendidas Tier 1 y 2 ---

def get_extended_quiver_signals(symbol):
    return {
        "has_recent_sec13f_activity": has_recent_sec13f_activity(symbol),
        "has_recent_sec13f_changes": has_recent_sec13f_changes(symbol),
        "has_recent_house_purchase": has_recent_house_purchase(symbol),
        "is_trending_on_twitter": is_trending_on_twitter(symbol),
        "has_positive_app_ratings": has_positive_app_ratings(symbol)
    }

def has_recent_sec13f_activity(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/sec13f")
    if not isinstance(data, list):
        return False

    return any((tx.get("Ticker") or "").upper() == symbol.upper() for tx in data)




def has_recent_sec13f_changes(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/sec13fchanges")
    if not isinstance(data, list):
        return False
    matches = [tx for tx in data if tx.get("Ticker") == symbol.upper()]
    return any(abs(tx.get("Change_Pct", 0)) >= 10 for tx in matches)

from datetime import datetime, timedelta

def has_recent_house_purchase(symbol):
    url = f"{QUIVER_BASE_URL}/live/housetrading"
    data = safe_quiver_request(url)

    if not isinstance(data, list):
        return False

    cutoff_date = datetime.utcnow() - timedelta(days=30)

    for tx in data:
        if tx.get("Ticker") != symbol.upper():
            continue
        if tx.get("Transaction") != "Purchase":
            continue

        try:
            tx_date = datetime.fromisoformat(tx.get("Date").replace("Z", ""))
            if tx_date >= cutoff_date:
                return True
        except Exception as e:
            print(f"⚠️ Error interpretando fecha en House Trading: {e}")
            continue

    return False



def is_trending_on_twitter(symbol):
    """
    Evalúa si el símbolo tiene una presencia significativa en Twitter.
    Umbral ajustado: al menos 5000 seguidores.
    """
    url = f"{QUIVER_BASE_URL}/live/twitter"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    
    matches = [tx for tx in data if tx.get("Ticker") == symbol.upper()]
    
    # Condición: al menos una entrada con >5000 seguidores
    return any(tx.get("Followers", 0) >= 5000 for tx in matches)



def has_positive_app_ratings(symbol):
    """
    Evalúa si la empresa tiene alguna app con valoración igual o superior a 4.0.
    No se requiere conteo mínimo de reviews para esta versión.
    """
    url = f"{QUIVER_BASE_URL}/live/appratings"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False

    matches = [tx for tx in data if tx.get("Ticker") == symbol.upper()]
    
    return any(tx.get("Rating", 0) >= 4.0 and tx.get("Count", 0) >= 20 for tx in matches)



