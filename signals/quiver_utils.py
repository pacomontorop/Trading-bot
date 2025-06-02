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
    "has_positive_app_ratings": 2,
    "has_recent_senate_purchase": 3,
    "has_recent_senate_sale": 3,
    "has_recent_lobbying": 2,
    "has_recent_corporate_flights": 2,
    "has_recent_political_beta": 1,
    "has_recent_wikipedia_views": 1,
    "has_recent_google_trends": 1,
    "has_recent_cnbc_mentions": 1,
    "has_recent_fda_approvals": 2,
    "has_recent_off_exchange_short_volume": 1,
    "has_recent_spacs_mentions": 1,
    "has_recent_reddit_mentions": 1,
    "has_recent_twitter_mentions": 1,
    "has_recent_glassdoor_reviews": 1,
    "has_recent_job_postings": 1,
    "has_recent_quiver_strategies": 1,
    "has_recent_quiver_alerts": 1,
    "has_recent_quiver_videos": 1,
    "has_recent_quiver_news": 1,
    "has_recent_quiver_blog": 1,
    "has_recent_quiver_tutorials": 1,
    "has_recent_quiver_faqs": 1,
    "has_recent_quiver_docs": 1,
    "has_recent_quiver_github": 1,
    "has_recent_quiver_api": 1,
    "has_recent_quiver_contact": 1,
    "has_recent_quiver_terms": 1,
    "has_recent_quiver_privacy": 1
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

def get_gov_contract_signal(symbol):
    url = f"{QUIVER_BASE_URL}/live/govcontracts"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False

    current_year = datetime.now().year

    symbol_contracts = [tx for tx in data if tx.get("Ticker") == symbol.upper()]

    for tx in symbol_contracts:
        try:
            amount = float(tx.get("Amount", "0").replace(",", "").replace("$", ""))
            year = int(tx.get("Year", 0))
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
    
    return any(tx.get("momentum", 0) >= 1.2 for tx in recent_entries)

def get_wsb_signal(symbol):
    url = f"{QUIVER_BASE_URL}/historical/wallstreetbets/{symbol.upper()}"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False

    recent_data = data[-5:] if len(data) >= 5 else data

    high_mention_days = [tx for tx in recent_data if tx.get("Mentions", 0) > 10]

    return len(high_mention_days) >= 1

def get_etf_flow_signal(symbol):
    url = f"{QUIVER_BASE_URL}/live/etfholdings?ticker={symbol.upper()}"
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
        "has_positive_app_ratings": has_positive_app_ratings(symbol),
        "has_recent_senate_purchase": has_recent_senate_purchase(symbol),
        "has_recent_senate_sale": has_recent_senate_sale(symbol),
        "has_recent_lobbying": has_recent_lobbying(symbol),
        "has_recent_corporate_flights": has_recent_corporate_flights(symbol),
        "has_recent_political_beta": has_recent_political_beta(symbol),
        "has_recent_wikipedia_views": has_recent_wikipedia_views(symbol),
        "has_recent_google_trends": has_recent_google_trends(symbol),
        "has_recent_cnbc_mentions": has_recent_cnbc_mentions(symbol),
        "has_recent_fda_approvals": has_recent_fda_approvals(symbol),
        "has_recent_off_exchange_short_volume": has_recent_off_exchange_short_volume(symbol),
        "has_recent_spacs_mentions": has_recent_spacs_mentions(symbol),
        "has_recent_reddit_mentions": has_recent_reddit_mentions(symbol),
        "has_recent_twitter_mentions": has_recent_twitter_mentions(symbol),
        "has_recent_glassdoor_reviews": has_recent_glassdoor_reviews(symbol),
        "has_recent_job_postings": has_recent_job_postings(symbol)
    }

def has_recent_sec13f_activity(symbol):
    url = f"{QUIVER_BASE_URL}/live/sec13f"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker", "").upper() == symbol.upper() for tx in data)

def has_recent_sec13f_changes(symbol):
    url = f"{QUIVER_BASE_URL}/live/sec13fchanges"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(abs(tx.get("Change_Pct", 0)) >= 10 and tx.get("Ticker", "").upper() == symbol.upper() for tx in data)

def has_recent_house_purchase(symbol):
    url = f"{QUIVER_BASE_URL}/live/housetrading"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    cutoff = datetime.utcnow() - timedelta(days=30)
    return any(
        tx.get("Ticker") == symbol.upper() and tx.get("Transaction") == "Purchase" and
        datetime.fromisoformat(tx.get("Date").replace("Z", "")) > cutoff
        for tx in data
    )

def is_trending_on_twitter(symbol):
    url = f"{QUIVER_BASE_URL}/live/twitter"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() and tx.get("Followers", 0) > 5000 for tx in data)

def has_positive_app_ratings(symbol):
    url = f"{QUIVER_BASE_URL}/live/appratings"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() and tx.get("Rating", 0) >= 4.0 and tx.get("Count", 0) >= 20 for tx in data)

def has_recent_senate_purchase(symbol):
    url = f"{QUIVER_BASE_URL}/live/senatetrading"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    cutoff = datetime.utcnow() - timedelta(days=90)
    return any(tx.get("Ticker") == symbol.upper() and tx.get("Transaction") == "Purchase" and
               datetime.fromisoformat(tx.get("Date").replace("Z", "")) > cutoff for tx in data)

def has_recent_senate_sale(symbol):
    url = f"{QUIVER_BASE_URL}/live/senatetrading"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    cutoff = datetime.utcnow() - timedelta(days=90)
    return any(tx.get("Ticker") == symbol.upper() and tx.get("Transaction") == "Sale" and
               datetime.fromisoformat(tx.get("Date").replace("Z", "")) > cutoff for tx in data)

def has_recent_lobbying(symbol):
    url = f"{QUIVER_BASE_URL}/live/lobbying"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() for tx in data)

def has_recent_corporate_flights(symbol):
    url = f"{QUIVER_BASE_URL}/live/corporateflights"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() for tx in data)

def has_recent_political_beta(symbol):
    url = f"{QUIVER_BASE_URL}/live/politicalbeta"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() and abs(tx.get("Beta", 0)) >= 0.1 for tx in data)

def has_recent_wikipedia_views(symbol):
    url = f"{QUIVER_BASE_URL}/live/wikipedia"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() and tx.get("Views", 0) > 1000 for tx in data)

def has_recent_google_trends(symbol):
    url = f"{QUIVER_BASE_URL}/live/googletrends"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() and tx.get("Value", 0) > 20 for tx in data)

def has_recent_cnbc_mentions(symbol):
    url = f"{QUIVER_BASE_URL}/live/cnbc"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() for tx in data)

def has_recent_fda_approvals(symbol):
    url = f"{QUIVER_BASE_URL}/live/fda"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() for tx in data)

def has_recent_off_exchange_short_volume(symbol):
    url = f"{QUIVER_BASE_URL}/live/offexchange"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() for tx in data)

def has_recent_spacs_mentions(symbol):
    url = f"{QUIVER_BASE_URL}/live/spacs"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() for tx in data)

def has_recent_reddit_mentions(symbol):
    url = f"{QUIVER_BASE_URL}/live/reddit"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() for tx in data)

def has_recent_twitter_mentions(symbol):
    url = f"{QUIVER_BASE_URL}/live/twitter"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() for tx in data)

def has_recent_glassdoor_reviews(symbol):
    url = f"{QUIVER_BASE_URL}/live/glassdoor"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() and tx.get("Rating", 0) >= 3.5 for tx in data)

def has_recent_job_postings(symbol):
    url = f"{QUIVER_BASE_URL}/live/jobpostings"
    data = safe_quiver_request(url)
    if not isinstance(data, list):
        return False
    return any(tx.get("Ticker") == symbol.upper() for tx in data)
































































































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



