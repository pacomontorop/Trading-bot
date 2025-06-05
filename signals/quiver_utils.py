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

# Pesos por señal para score final
QUIVER_SIGNAL_WEIGHTS = {
    # Tier 1
    "insider_buy_more_than_sell": 3,
    "has_gov_contract": 3,
    "positive_patent_momentum": 2,
    "trending_wsb": 2,
    "bullish_etf_flow": 2,
    "has_recent_sec13f_activity": 2,
    "has_recent_sec13f_changes": 2,
    "has_recent_house_purchase": 2,
    "is_trending_on_twitter": 1,
    # Tier 2
    "has_positive_app_ratings": 2
}

QUIVER_APPROVAL_THRESHOLD = 2

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
    from signals.filters import is_approved_by_finnhub_and_alphavantage
    if is_approved_by_finnhub_and_alphavantage(symbol):
        log_event(f"✅ {symbol} aprobado por fallback: Finnhub + Alpha.")
        return True
    print(f"⛔ {symbol} no aprobado por Quiver ni por Finnhub + Alpha.")
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
    for key, value in signals.items():
        status = "✅" if value else "❌"
        print(f"   {status} {key}: {value}")
    score = sum(QUIVER_SIGNAL_WEIGHTS.get(k, 0) for k, v in signals.items() if v)
    active_signals = [k for k, v in signals.items() if v]
    print(f"🧠 {symbol} → score: {score} (umbral: {QUIVER_APPROVAL_THRESHOLD})")
    if score >= QUIVER_APPROVAL_THRESHOLD:
        log_event(f"✅ {symbol} aprobado con score {score}. Activas: {', '.join(active_signals)}")
        return True
    else:
        print(f"⛔ {symbol} no aprobado. Score: {score}. Activas: {', '.join(active_signals)}")
        return False

def safe_quiver_request(url, retries=3, delay=2):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.ok:
                return r.json()
            else:
                print(f"⚠️ Respuesta inesperada en {url}: código {r.status_code}")
        except Exception as e:
            print(f"⚠️ Error en {url}: {e}")
        wait = delay * (2 ** i)  # backoff exponencial
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
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/insiders")
    if not isinstance(data, list):
        return False
    cutoff = datetime.utcnow() - timedelta(days=7)
    entries = [d for d in data if d.get("Ticker") == symbol.upper()]
    buys = sum(1 for d in entries if d["TransactionCode"] == "P" and datetime.fromisoformat(d["Date"].replace("Z", "")) > cutoff)
    sells = sum(1 for d in entries if d["TransactionCode"] == "S")
    return buys >= sells

def get_gov_contract_signal(symbol):
    data = safe_quiver_request(f"{QUIVER_BASE_URL}/live/govcontracts")
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
    return any(abs(d.get("Change_Pct", 0)) >= 5 for d in data if d.get("Ticker") == symbol.upper())

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


