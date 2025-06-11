#quiver_utils.py


import os
import time
import requests
from dotenv import load_dotenv
from utils.logger import log_event
from datetime import datetime, timedelta

load_dotenv()

# Variables globales para caché de endpoints grandes
INSIDERS_DATA = None
GOVCONTRACTS_DATA = None


QUIVER_API_KEY = os.getenv("QUIVER_API_KEY")
QUIVER_BASE_URL = "https://api.quiverquant.com/beta"
HEADERS = {"Authorization": f"Bearer {QUIVER_API_KEY}"}


# Pesos por señal para score final
QUIVER_SIGNAL_WEIGHTS = {
    "insider_buy_more_than_sell": 5,
    "has_gov_contract": 4,
    "positive_patent_momentum": 3,
    "has_recent_sec13f_activity": 3,
    "has_recent_sec13f_changes": 3,
    "trending_wsb": 1,
    "bullish_etf_flow": 1,
    "has_recent_house_purchase": 1,
    "is_trending_on_twitter": 1,
    "has_positive_app_ratings": 1
}
QUIVER_APPROVAL_THRESHOLD = 6  # o 7


def is_approved_by_quiver(symbol):
    try:
        signals = get_all_quiver_signals(symbol)
        return evaluate_quiver_signals(signals, symbol)
    except Exception as e:
        print(f"⛔ {symbol} no aprobado por Quiver debido a error: {e}")
        log_event(f"⛔ {symbol} no aprobado por Quiver debido a error: {e}")
        return False


def get_all_quiver_signals(symbol):
    basic_signals = get_quiver_signals(symbol)
    extended_signals = get_extended_quiver_signals(symbol)
    combined_signals = {**basic_signals, **extended_signals}
    combined_signals["has_political_pressure"] = has_political_pressure(symbol)
    combined_signals["has_social_demand"] = has_social_demand(symbol)
    log_event(f"🧠 {symbol} señales combinadas: {combined_signals}")
    return combined_signals

def score_quiver_signals(signals):
    score = 0
    for key, active in signals.items():
        if active:
            score += QUIVER_SIGNAL_WEIGHTS.get(key, 0)
    return score

def evaluate_quiver_signals(signals, symbol=""):
    print(f"\n🧪 Evaluando señales Quiver para {symbol}...")
    
    # Mostrar todas las señales con su estado
    for key, value in signals.items():
        status = "✅" if value else "❌"
        print(f"   {status} {key}: {value}")
    
    # Calcular el score final sumando los pesos de las señales activas
    score = sum(QUIVER_SIGNAL_WEIGHTS.get(k, 0) for k, v in signals.items() if v)
    
    # Contar cuántas señales activas tiene
    active_signals = [k for k, v in signals.items() if v]
    active_signals_count = len(active_signals)
    
    # Mostrar resumen
    print(f"🧠 {symbol} → score: {score} (umbral: {QUIVER_APPROVAL_THRESHOLD}), señales activas: {active_signals_count}")
    
    # Verificar si aprueba por score y número de señales mínimas (ej. mínimo 3 señales)
    if score >= QUIVER_APPROVAL_THRESHOLD and active_signals_count >= 3:
        log_event(f"✅ {symbol} aprobado con score {score}. Activas: {', '.join(active_signals)}")
        return True
    else:
        print(f"⛔ {symbol} no aprobado. Score: {score}, señales activas: {active_signals_count}")
        return False

def safe_quiver_request(url, retries=3, delay=2):
    print(f"🔑 Usando clave Quiver: {repr(QUIVER_API_KEY)}")  # 👈 LOG DE LA CLAVE
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.ok:
                return r.json()
            else:
                print(f"⚠️ Respuesta inesperada en {url}: código {r.status_code}")
        except Exception as e:
            print(f"⚠️ Error en {url}: {e}")
        wait = delay * (2 ** i)
        print(f"🔄 Reintentando en {wait}s...")
        time.sleep(wait)
    print(f"❌ Fallo final en {url}. Se devuelve None.")
    return None



def get_quiver_signals(symbol):
    return {
        "insider_buy_more_than_sell": get_insider_signal(symbol),
        "has_gov_contract": get_gov_contract_signal(symbol),
        "positive_patent_momentum": get_patent_momentum_signal(symbol),
        "trending_wsb": get_wsb_signal(symbol),
        "bullish_etf_flow": get_etf_flow_signal(symbol)
    }

def get_insider_signal(symbol):
    global INSIDERS_DATA
    if INSIDERS_DATA is None:
        INSIDERS_DATA = safe_quiver_request(f"{QUIVER_BASE_URL}/live/insiders")
    data = INSIDERS_DATA
    if not isinstance(data, list):
        return False
    
    # Filtrar operaciones del símbolo en los últimos 7 días
    cutoff = datetime.utcnow() - timedelta(days=7)
    entries = [d for d in data if d.get("Ticker") == symbol.upper()]
    
    # Contar compras y ventas recientes
    recent_buys = sum(1 for d in entries if d["TransactionCode"] == "P" and datetime.fromisoformat(d["Date"].replace("Z", "")) > cutoff)
    recent_sells = sum(1 for d in entries if d["TransactionCode"] == "S" and datetime.fromisoformat(d["Date"].replace("Z", "")) > cutoff)
    
    # Más estricto: al menos 2 compras recientes y que superen en el doble las ventas
    if recent_buys >= 2 and recent_buys >= 2 * recent_sells:
        return True
    return False



def get_gov_contract_signal(symbol):
    global GOVCONTRACTS_DATA
    if GOVCONTRACTS_DATA is None:
        GOVCONTRACTS_DATA = safe_quiver_request(f"{QUIVER_BASE_URL}/live/govcontracts")
    data = GOVCONTRACTS_DATA
    if not isinstance(data, list):
        return False
    current_year = datetime.now().year
    for d in data:
        if d.get("Ticker") == symbol.upper():
            try:
                amt = float(d.get("Amount", "0").replace("$", "").replace(",", ""))
                if amt >= 100_000 and int(d.get("Year", 0)) >= current_year - 1:
                    return True
            except:
                continue
    return False


def get_patent_momentum_signal(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/patentmomentum")
    if not isinstance(data, list):
        return False
    return any(d["ticker"] == symbol.upper() and d.get("momentum", 0) >= 1 for d in data)

def get_wsb_signal(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/historical/wallstreetbets/{symbol.upper()}")
    if not isinstance(data, list):
        return False
    return any(d.get("Mentions", 0) >= 10 for d in data[-5:])

def get_etf_flow_signal(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/etfholdings?ticker={symbol.upper()}")
    if not isinstance(data, list):
        return False
    total = sum(d.get("Value ($)", 0) for d in data)
    return total > 250_000

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
    return any(d.get("Ticker") == symbol.upper() for d in data)

def has_recent_sec13f_changes(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/sec13fchanges")
    if not isinstance(data, list):
        return False

    for d in data:
        if d.get("Ticker") == symbol.upper():
            pct = d.get("Change_Pct")
            if pct is not None and abs(pct) >= 5:
                return True
    return False


def has_recent_house_purchase(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/housetrading")
    if not isinstance(data, list):
        return False
    cutoff = datetime.utcnow() - timedelta(days=30)
    return any(d.get("Ticker") == symbol.upper() and d.get("Transaction") == "Purchase" and datetime.fromisoformat(d["Date"].replace("Z", "")) >= cutoff for d in data)

def is_trending_on_twitter(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/twitter")
    if not isinstance(data, list):
        return False
    return any(d.get("Ticker") == symbol.upper() and d.get("Followers", 0) >= 5000 for d in data)

def has_positive_app_ratings(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/appratings")
    if not isinstance(data, list):
        return False
    return any(d.get("Ticker") == symbol.upper() and d.get("Rating", 0) >= 4.0 and d.get("Count", 0) >= 10 for d in data)

# Indicadores compuestos
def has_political_pressure(symbol):
    return get_gov_contract_signal(symbol) or has_recent_house_purchase(symbol)

def has_social_demand(symbol):
    return get_wsb_signal(symbol) or is_trending_on_twitter(symbol)

def initialize_quiver_caches():
    """
    Inicializa los datos pesados de Quiver para ser usados localmente.
    Evita llamadas repetidas a la API para datos grandes.
    """
    global INSIDERS_DATA, GOVCONTRACTS_DATA
    if INSIDERS_DATA is None:
        print("🔄 Descargando datos de insiders...")
        INSIDERS_DATA = safe_quiver_request(f"{QUIVER_BASE_URL}/live/insiders")
        print(f"✅ INSIDERS_DATA cargado: {len(INSIDERS_DATA) if isinstance(INSIDERS_DATA, list) else 'Error'}")
    if GOVCONTRACTS_DATA is None:
        print("🔄 Descargando datos de contratos gubernamentales...")
        GOVCONTRACTS_DATA = safe_quiver_request(f"{QUIVER_BASE_URL}/live/govcontracts")
        print(f"✅ GOVCONTRACTS_DATA cargado: {len(GOVCONTRACTS_DATA) if isinstance(GOVCONTRACTS_DATA, list) else 'Error'}")


