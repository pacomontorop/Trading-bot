#quiver_utils.py


import os
import time
import random
import requests
import asyncio
import math
from dataclasses import dataclass
from .quiver_event_loop import run_in_quiver_loop
from dotenv import load_dotenv
from utils.logger import log_event, log_once
from utils.cache import get as cache_get, set as cache_set
import config
from datetime import datetime, timedelta, timezone
from typing import Optional
from signals.quiver_throttler import throttled_request
from signals.fmp_utils import price_target_news


class QuiverRateLimitError(Exception):
    """Raised when the Quiver API responds with a rate limit."""


class QuiverTemporaryError(Exception):
    """Raised when a transient error should suppress further requests."""



load_dotenv()

# TTL helpers for caching
def _ttl_lot() -> int:
    cfg = getattr(config, "_policy", {}) or {}
    return int(((cfg.get("cache") or {}).get("lot_ttl_sec", 900)))


def _ttl_heavy() -> int:
    cfg = getattr(config, "_policy", {}) or {}
    cache_cfg = cfg.get("cache") or {}
    return int(cache_cfg.get("quiver_heavy_ttl_sec", _ttl_lot()))


_ENDPOINT_SUPPRESS: dict[str, float] = {}


def _cached_heavy_endpoint(name: str, url: str, ttl: int):
    key = f"HE_{name}"
    data = cache_get(key, ttl)
    if data is not None:
        return data
    now = time.time()
    suppressed_until = _ENDPOINT_SUPPRESS.get(name)
    if suppressed_until and now < suppressed_until:
        log_once(
            f"quiver_suppressed_{name}",
            f"CACHE {name}: salto por suppress hasta {suppressed_until:.0f}",
            min_interval_sec=60,
        )
        return None
    try:
        data = safe_quiver_request(url)
    except QuiverRateLimitError:
        _ENDPOINT_SUPPRESS[name] = now + ttl
        log_event(
            f"CACHE {name}: suppress por rate limit durante {ttl}s",
            event="CACHE",
        )
        return None
    except QuiverTemporaryError:
        _ENDPOINT_SUPPRESS[name] = now + ttl
        log_event(
            f"CACHE {name}: suppress temporal durante {ttl}s",
            event="CACHE",
        )
        return None
    if isinstance(data, list):
        cache_set(key, data)
    return data


QUIVER_API_KEY = os.getenv("QUIVER_API_KEY")
QUIVER_BASE_URL = "https://api.quiverquant.com/beta"
HEADERS = {"Authorization": f"Bearer {QUIVER_API_KEY}"}
QUIVER_TIMEOUT = int(os.getenv("QUIVER_TIMEOUT", "30"))

# Track which symbols have been approved and logged today
approved_today = set()

def reset_daily_approvals():
    """Clear daily approval log."""
    approved_today.clear()


# Pesos por señal para score final
QUIVER_SIGNAL_WEIGHTS = {
    "insider_buy_more_than_sell": 5,
    "has_gov_contract": 4,
    "positive_patent_momentum": 3,
    "has_recent_sec13f_activity": 3,
    "has_recent_sec13f_changes": 3,
    "trending_wsb": 0.5,
    "bullish_price_target": 0.5,
    "has_recent_house_purchase": 1,
    "is_trending_on_twitter": 0.5,
    "has_positive_app_ratings": 0.5
}
# Lowered threshold to allow more opportunities while maintaining some rigor
QUIVER_APPROVAL_THRESHOLD = 5

# Weights for determining whether a ticker has sufficiently recent activity.
# Each key represents a different Quiver endpoint used to detect "eventos recientes".
RECENT_EVENT_WEIGHTS = {
    "insider": 2,
    "house_trade": 1,
    "senate_trade": 1,
    "congress_trade": 1,
    "historical_congress": 0.5,
    "historical_senate": 0.5,
    "gov_contract": 1,
    "gov_contract_all": 1,
    "lobbying": 0.5,
    "off_exchange": 0.5,
    "sec13f": 0.5,
    "sec13f_changes": 0.5,
    "price_target_news": 0.25,
    "patent_drift": 0.5,
    "patent_momentum": 0.5,
    "recent_patents": 0.5,
}

# Minimum score to consider that a symbol has recent activity.
RECENT_EVENT_THRESHOLD = 1


@dataclass
class SignalResult:
    active: bool
    days: Optional[float] = None

    def __bool__(self):  # pragma: no cover - simple delegator
        return self.active


def _days_since(dt: datetime | None) -> float | None:
    if not dt:
        return None
    try:
        from datetime import timezone, datetime as _dt
        now = _dt.utcnow().replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (now - dt).total_seconds() / 86400.0
        return max(0.0, days)
    except Exception:
        return None


def recency_weight(days_since_event: Optional[float], k: float = 2.0, decay: float = 0.1) -> float:
    """Return a multiplier giving extra weight to recent events.

    Events in the last two days get a fixed boost ``k``.  Older events decay
    exponentially so they still contribute, but progressively less.
    """
    if days_since_event is None:
        return 1.0
    if days_since_event <= 2:
        return k
    return math.exp(-decay * (days_since_event - 2))


async def _async_is_approved_by_quiver(symbol):  # pragma: no cover - backward compat
    """Fetch and evaluate Quiver signals for ``symbol``.

    Historically this function returned a boolean indicating approval. It now
    returns a dictionary with scoring information so that approval decisions
    can be made elsewhere.
    """

    print(f"🔎 Checking {symbol}...", flush=True)
    try:
        signals = await asyncio.to_thread(get_all_quiver_signals, symbol)
        return evaluate_quiver_signals(signals, symbol)
    except Exception as e:  # pragma: no cover - network/parse errors
        msg = f"⛔ {symbol} no aprobado por Quiver debido a error: {e}"
        print(msg)
        log_event(msg)
        return {"score": 0.0, "active_signals": []}


def is_approved_by_quiver(symbol):  # pragma: no cover - backward compat
    """Synchronous wrapper for :func:`_async_is_approved_by_quiver`.

    Returns the same evaluation dictionary produced by
    :func:`evaluate_quiver_signals`.
    """

    return run_in_quiver_loop(_async_is_approved_by_quiver(symbol))


def get_all_quiver_signals(symbol):
    """Retrieve all Quiver signals along with their recency information."""
    basic_signals = get_quiver_signals(symbol)
    extended_signals = get_extended_quiver_signals(symbol)
    combined_signals = {**basic_signals, **extended_signals}
    combined_signals["has_political_pressure"] = has_political_pressure(symbol)
    combined_signals["has_social_demand"] = has_social_demand(symbol)
    log_event(f"🧠 {symbol} señales combinadas: {combined_signals}")
    return combined_signals


def fetch_quiver_signals(symbol):
    cfg = getattr(config, "_policy", {}) or {}
    ttl = int(((cfg.get("cache") or {}).get("symbol_ttl_sec", 600)))
    k = f"Q_SIG:{symbol.upper()}"
    v = cache_get(k, ttl)
    if v is not None:
        return v
    res = get_all_quiver_signals(symbol)
    cache_set(k, res)
    return res

def score_quiver_signals(signals):
    """Calculate final score applying recency weighting to each active signal."""
    score = 0.0
    for key, value in signals.items():
        if isinstance(value, SignalResult):
            active = value.active
            days = value.days
        elif isinstance(value, dict):
            active = value.get("active", False)
            days = value.get("days")
        else:
            active = bool(value)
            days = None
        if active:
            weight = QUIVER_SIGNAL_WEIGHTS.get(key, 0)
            weight *= recency_weight(days)
            score += weight
    return score


def get_adaptive_take_profit(
    symbol: str, entry_price: float, quiver_score: float
) -> Optional[float]:
    """Calcula un take profit dinámico basado en el quiver_score."""
    if quiver_score >= 10:
        pct = 0.10
    elif quiver_score >= 7:
        pct = 0.07
    elif quiver_score >= 5:
        pct = 0.045
    else:
        return None

    take_profit = round(entry_price * (1 + pct), 2)
    print(
        f"🎯 {symbol} take profit fijado en ${take_profit:.2f} (score {quiver_score})"
    )
    return take_profit


# Use a short two-day lookback to ensure only very fresh events are considered
def has_recent_quiver_event(symbol, days=2):
    """Score recent activity for ``symbol`` across multiple Quiver endpoints."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    score = 0

    if has_recent_insider_trade(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["insider"]
    if has_recent_house_purchase(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["house_trade"]
    if has_recent_senate_trade(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["senate_trade"]
    if has_recent_congress_trade(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["congress_trade"]
    if has_historical_congress_trade(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["historical_congress"]
    if has_historical_senate_trade(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["historical_senate"]
    if has_recent_gov_contract(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["gov_contract"]
    if has_recent_gov_contract_all(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["gov_contract_all"]
    if has_recent_lobbying(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["lobbying"]
    if has_recent_off_exchange(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["off_exchange"]
    if has_recent_sec13f_activity(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["sec13f"]
    if has_recent_sec13f_changes(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["sec13f_changes"]
    if has_recent_price_target_news(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["price_target_news"]
    if has_recent_patent_drift(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["patent_drift"]
    if has_recent_patent_momentum(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["patent_momentum"]
    if has_recent_patents(symbol, cutoff):
        score += RECENT_EVENT_WEIGHTS["recent_patents"]

    return score >= RECENT_EVENT_THRESHOLD


def evaluate_quiver_signals(signals, symbol=""):
    """Return scoring info for Quiver signals without making approval decisions."""

    print(f"\n🧪 Evaluando señales Quiver para {symbol}...")

    active_signals = []
    for key, value in signals.items():
        if isinstance(value, SignalResult):
            active = value.active
            days = value.days
        elif isinstance(value, dict):
            active = value.get("active", False)
            days = value.get("days")
        else:
            active = bool(value)
            days = None
        status = "✅" if active else "❌"
        age = f" ({days:.1f}d)" if days is not None else ""
        print(f"   {status} {key}: {active}{age}")
        if active:
            active_signals.append(key)

    score = score_quiver_signals(signals)
    info = {"score": score, "active_signals": active_signals}
    print(f"🧠 {symbol} → score: {score:.2f}, señales activas: {len(active_signals)}")
    return info


def safe_quiver_request(url, retries=5, delay=4):
    # Log only that the key is present without revealing it
    if QUIVER_API_KEY:
        log_once(
            "quiver_api_key_present",
            "🔑 Usando clave Quiver: [REDACTED]",
            min_interval_sec=3600,
        )
    else:
        log_once(
            "quiver_api_key_missing",
            "🔑 Advertencia: QUIVER_API_KEY no configurada",
            min_interval_sec=3600,
        )
    last_error: Optional[Exception] = None
    for i in range(retries):
        try:
            r = throttled_request(requests.get, url, headers=HEADERS, timeout=QUIVER_TIMEOUT)
            if r.ok:
                return r.json()
            # Retrys are only useful for rate limits; for other HTTP errors we
            # stop early to avoid spamming the logs with exponential backoff
            # messages.
            if r.status_code == 429:
                last_error = QuiverRateLimitError("rate_limit")
                wait = delay * (2 ** i)
                wait += random.uniform(0, delay)
                print(f"⚠️ Límite de velocidad alcanzado en {url}: código {r.status_code}")
                print(f"🔄 Reintentando en {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                last_error = QuiverTemporaryError(f"server_{r.status_code}")
                print(f"⚠️ Error del servidor en {url}: código {r.status_code}")
                break
            if r.status_code == 404:
                print(f"ℹ️ Datos no encontrados en {url}")
                return []
            last_error = QuiverTemporaryError(f"http_{r.status_code}")
            print(f"⚠️ Respuesta inesperada en {url}: código {r.status_code}")
            break
        except requests.exceptions.Timeout:
            last_error = QuiverTemporaryError("timeout")
            print(f"⏱️ Timeout en {url} tras {QUIVER_TIMEOUT}s")
        except Exception as e:
            last_error = QuiverTemporaryError(str(e))
            print(f"⚠️ Error en {url}: {e}")
        wait = delay * (2 ** i)
        wait += random.uniform(0, delay)
        print(f"🔄 Reintentando en {wait}s...")
        time.sleep(wait)
    print(f"❌ Fallo final en {url}. Se devuelve None.")
    if last_error:
        raise last_error
    return None


def _request_or_default(url: str, default=None):
    """Fetch data from Quiver while tolerating transient failures."""

    try:
        return safe_quiver_request(url)
    except (QuiverRateLimitError, QuiverTemporaryError):
        return default


def get_quiver_signals(symbol):
    return {
        "insider_buy_more_than_sell": get_insider_signal(symbol),
        "has_gov_contract": get_gov_contract_signal(symbol),
        "positive_patent_momentum": get_patent_momentum_signal(symbol),
        "trending_wsb": get_wsb_signal(symbol),
        "bullish_price_target": get_price_target_signal(symbol),
    }

def get_insider_signal(symbol):
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint(
        "live_insiders", f"{QUIVER_BASE_URL}/live/insiders", ttl
    )
    if not isinstance(data, list):
        return SignalResult(False, None)

    entries = [d for d in data if d.get("Ticker") == symbol.upper()]

    recent_buys = 0
    recent_sells = 0
    latest_buy = None
    for d in entries:
        try:
            event_date = datetime.fromisoformat(d["Date"].replace("Z", "")).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if d["TransactionCode"] == "P":
            recent_buys += 1
            if latest_buy is None or event_date > latest_buy:
                latest_buy = event_date
        elif d["TransactionCode"] == "S":
            recent_sells += 1

    if recent_buys >= 2 and recent_buys >= 2 * recent_sells and latest_buy:
        days = _days_since(latest_buy)
        return SignalResult(True, days)
    return SignalResult(False, None)



def get_gov_contract_signal(symbol):
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint(
        "live_govcontracts", f"{QUIVER_BASE_URL}/live/govcontracts", ttl
    )
    if not isinstance(data, list):
        return SignalResult(False, None)
    latest = None
    for d in data:
        if d.get("Ticker") == symbol.upper():
            try:
                amt = float(d.get("Amount", "0").replace("$", "").replace(",", ""))
            except Exception:
                continue
            date_str = d.get("Date") or d.get("AnnouncementDate")
            if not date_str:
                continue
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", "")).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if amt >= 100_000 and (latest is None or event_date > latest):
                latest = event_date
    if latest:
        days = _days_since(latest)
        return SignalResult(True, days)
    return SignalResult(False, None)


def get_patent_momentum_signal(symbol):
    data = _request_or_default(f"{QUIVER_BASE_URL}/live/patentmomentum")
    if not isinstance(data, list):
        return SignalResult(False, None)
    latest = None
    for d in data:
        if d.get("ticker") == symbol.upper() and isinstance(d.get("momentum"), (int, float)) and d["momentum"] >= 1:
            date_str = d.get("date") or d.get("Date")
            if not date_str:
                continue
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", "")).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if latest is None or event_date > latest:
                latest = event_date
    if latest:
        days = _days_since(latest)
        return SignalResult(True, days)
    return SignalResult(False, None)


def get_wsb_signal(symbol):
    data = _request_or_default(
        f"{QUIVER_BASE_URL}/historical/wallstreetbets/{symbol.upper()}"
    )
    if not isinstance(data, list):
        return SignalResult(False, None)
    latest = None
    for d in data[-5:]:
        date_str = d.get("Date") or d.get("date")
        if not date_str:
            continue
        try:
            event_date = datetime.fromisoformat(str(date_str).replace("Z", "")).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if d.get("Mentions", 0) >= 10 and (latest is None or event_date > latest):
            latest = event_date
    if latest:
        days = _days_since(latest)
        return SignalResult(True, days)
    return SignalResult(False, None)

def get_price_target_signal(symbol):
    data = price_target_news(symbol, limit=5)
    if not isinstance(data, list):
        return SignalResult(False, None)
    latest = None
    for item in data:
        date_str = item.get("publishedDate")
        target = item.get("priceTarget")
        posted = item.get("priceWhenPosted")
        if not date_str or not isinstance(target, (int, float)) or not isinstance(posted, (int, float)):
            continue
        try:
            pub_date = datetime.fromisoformat(str(date_str).replace("Z", "")).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if target >= posted * 1.05 and (latest is None or pub_date > latest):
            latest = pub_date
    if latest:
        days = _days_since(latest)
        return SignalResult(True, days)
    return SignalResult(False, None)

def get_extended_quiver_signals(symbol):
    return {
        "has_recent_sec13f_activity": sec13f_activity_signal(symbol),
        "has_recent_sec13f_changes": sec13f_changes_signal(symbol),
        "has_recent_house_purchase": house_purchase_signal(symbol),
        "is_trending_on_twitter": twitter_trending_signal(symbol),
        "has_positive_app_ratings": app_ratings_signal(symbol),
    }


def sec13f_activity_signal(symbol):
    data = _request_or_default(f"{QUIVER_BASE_URL}/live/sec13f")
    if not isinstance(data, list):
        return SignalResult(False, None)
    latest = None
    for d in data:
        if d.get("Ticker") == symbol.upper():
            date_str = d.get("ReportDate") or d.get("Date")
            if not date_str:
                continue
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", "")).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if latest is None or event_date > latest:
                latest = event_date
    if latest:
        days = _days_since(latest)
        return SignalResult(True, days)
    return SignalResult(False, None)


def sec13f_changes_signal(symbol):
    data = _request_or_default(f"{QUIVER_BASE_URL}/live/sec13fchanges")
    if not isinstance(data, list):
        return SignalResult(False, None)
    latest = None
    for d in data:
        if d.get("Ticker") == symbol.upper():
            try:
                pct = d.get("Change_Pct")
                date_str = d.get("ReportDate") or d.get("Date")
                if date_str:
                    event_date = datetime.fromisoformat(str(date_str).replace("Z", "")).replace(tzinfo=timezone.utc)
                else:
                    event_date = None
                if isinstance(pct, (int, float)) and abs(pct) >= 5:
                    if event_date is not None and (latest is None or event_date > latest):
                        latest = event_date
            except Exception:
                continue
    if latest:
        days = _days_since(latest)
        return SignalResult(True, days)
    return SignalResult(False, None)


def house_purchase_signal(symbol):
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint(
        "live_housetrading", f"{QUIVER_BASE_URL}/live/housetrading", ttl
    )
    if not isinstance(data, list):
        return SignalResult(False, None)
    latest = None
    for d in data:
        if d.get("Ticker") == symbol.upper() and d.get("Transaction") == "Purchase":
            try:
                event_date = datetime.fromisoformat(d["Date"].replace("Z", "")).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if latest is None or event_date > latest:
                latest = event_date
    if latest:
        days = _days_since(latest)
        return SignalResult(True, days)
    return SignalResult(False, None)


def twitter_trending_signal(symbol):
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint("live_twitter", f"{QUIVER_BASE_URL}/live/twitter", ttl)
    if not isinstance(data, list):
        return SignalResult(False, None)
    latest = None
    for d in data:
        if d.get("Ticker") != symbol.upper():
            continue
        date_str = d.get("Date") or d.get("date")
        if date_str:
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", "")).replace(tzinfo=timezone.utc)
            except Exception:
                event_date = None
        else:
            event_date = None
        if isinstance(d.get("Followers"), (int, float)) and d["Followers"] >= 5000:
            if event_date and (latest is None or event_date > latest):
                latest = event_date
    if latest:
        days = _days_since(latest)
        return SignalResult(True, days)
    return SignalResult(False, None)


def app_ratings_signal(symbol):
    data = _request_or_default(f"{QUIVER_BASE_URL}/live/appratings")
    if not isinstance(data, list):
        return SignalResult(False, None)
    latest = None
    for d in data:
        if d.get("Ticker") != symbol.upper():
            continue
        date_str = d.get("Date") or d.get("date")
        if date_str:
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", "")).replace(tzinfo=timezone.utc)
            except Exception:
                event_date = None
        else:
            event_date = None
        if (
            isinstance(d.get("Rating"), (int, float))
            and d["Rating"] >= 4.0
            and isinstance(d.get("Count"), (int, float))
            and d["Count"] >= 10
        ):
            if event_date and (latest is None or event_date > latest):
                latest = event_date
    if latest:
        days = _days_since(latest)
        return SignalResult(True, days)
    return SignalResult(False, None)

def has_recent_sec13f_activity(symbol, cutoff=None):
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint("live_sec13f", f"{QUIVER_BASE_URL}/live/sec13f", ttl)
    if not isinstance(data, list):
        return False
    if cutoff is None:
        cutoff = datetime.utcnow() - timedelta(days=2)
    for d in data:
        if d.get("Ticker") == symbol.upper():
            date_str = d.get("ReportDate") or d.get("Date")
            if not date_str:
                continue
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
            except Exception:
                continue
            if event_date >= cutoff:
                return True
    return False

def has_recent_sec13f_changes(symbol, cutoff=None):
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint(
        "live_sec13fchanges", f"{QUIVER_BASE_URL}/live/sec13fchanges", ttl
    )
    if not isinstance(data, list):
        return False
    if cutoff is None:
        cutoff = datetime.utcnow() - timedelta(days=2)

    for d in data:
        if d.get("Ticker") == symbol.upper():
            try:
                pct = d.get("Change_Pct")
                date_str = d.get("ReportDate") or d.get("Date")
                event_date = None
                if date_str:
                    event_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
                if isinstance(pct, (int, float)) and abs(pct) >= 5 and (event_date is None or event_date >= cutoff):
                    return True
            except Exception as e:
                print(f"⚠️ Error procesando sec13fchanges para {symbol}: {e}")
    return False



def has_recent_house_purchase(symbol, cutoff=None):
    """Check recent purchases from U.S. House representatives."""
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint(
        "live_housetrading", f"{QUIVER_BASE_URL}/live/housetrading", ttl
    )
    if not isinstance(data, list):
        return False
    if cutoff is None:
        cutoff = datetime.utcnow() - timedelta(days=2)
    return any(
        d.get("Ticker") == symbol.upper()
        and d.get("Transaction") == "Purchase"
        and datetime.fromisoformat(d["Date"].replace("Z", "")) >= cutoff
        for d in data
    )


def has_recent_insider_trade(symbol, cutoff):
    """Recent insider transactions for the given symbol."""
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint(
        "live_insiders", f"{QUIVER_BASE_URL}/live/insiders", ttl
    )
    if not isinstance(data, list):
        return False
    for d in data:
        if d.get("Ticker") == symbol.upper():
            try:
                event_date = datetime.fromisoformat(d["Date"].replace("Z", ""))
                if event_date >= cutoff:
                    return True
            except Exception:
                continue
    return False


def has_recent_senate_trade(symbol, cutoff):
    data = _request_or_default(f"{QUIVER_BASE_URL}/live/senatetrading")
    if not isinstance(data, list):
        return False
    for d in data:
        if d.get("Ticker") == symbol.upper():
            try:
                event_date = datetime.fromisoformat(d["Date"].replace("Z", ""))
                if event_date >= cutoff:
                    return True
            except Exception:
                continue
    return False


def has_recent_congress_trade(symbol, cutoff):
    data = _request_or_default(f"{QUIVER_BASE_URL}/live/congresstrading")
    if not isinstance(data, list):
        return False
    for d in data:
        if d.get("Ticker") == symbol.upper():
            try:
                event_date = datetime.fromisoformat(d["Date"].replace("Z", ""))
                if event_date >= cutoff:
                    return True
            except Exception:
                continue
    return False


def has_historical_congress_trade(symbol, cutoff):
    data = _request_or_default(
        f"{QUIVER_BASE_URL}/historical/congresstrading/{symbol.upper()}"
    )
    if not isinstance(data, list):
        return False
    for d in data:
        date_str = d.get("TransactionDate") or d.get("Date")
        if not date_str:
            continue
        try:
            event_date = datetime.fromisoformat(date_str.replace("Z", ""))
            if event_date >= cutoff:
                return True
        except Exception:
            continue
    return False


def has_historical_senate_trade(symbol, cutoff):
    data = _request_or_default(
        f"{QUIVER_BASE_URL}/historical/senatetrading/{symbol.upper()}"
    )
    if not isinstance(data, list):
        return False
    for d in data:
        date_str = d.get("TransactionDate") or d.get("Date")
        if not date_str:
            continue
        try:
            event_date = datetime.fromisoformat(date_str.replace("Z", ""))
            if event_date >= cutoff:
                return True
        except Exception:
            continue
    return False


def has_recent_gov_contract(symbol, cutoff):
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint(
        "live_govcontracts", f"{QUIVER_BASE_URL}/live/govcontracts", ttl
    )
    if not isinstance(data, list):
        return False
    for d in data:
        if d.get("Ticker") == symbol.upper():
            date_str = d.get("Date") or d.get("AnnouncementDate")
            if not date_str:
                continue
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
            except Exception:
                continue
            if event_date >= cutoff:
                return True
    return False


def has_recent_gov_contract_all(symbol, cutoff):
    data = _request_or_default(f"{QUIVER_BASE_URL}/live/govcontractsall")
    if not isinstance(data, list):
        return False
    for d in data:
        if d.get("Ticker") == symbol.upper():
            date_str = d.get("Date") or d.get("AnnouncementDate")
            if not date_str:
                continue
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
            except Exception:
                continue
            if event_date >= cutoff:
                return True
    return False


def has_recent_lobbying(symbol, cutoff):
    data = _request_or_default(f"{QUIVER_BASE_URL}/live/lobbying")
    if not isinstance(data, list):
        return False
    for d in data:
        if d.get("Ticker") == symbol.upper():
            date_str = d.get("Date") or d.get("ReportDate")
            if not date_str:
                return True
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
                if event_date >= cutoff:
                    return True
            except Exception:
                continue
    return False


def has_recent_off_exchange(symbol, cutoff):
    data = _request_or_default(f"{QUIVER_BASE_URL}/live/offexchange")
    if not isinstance(data, list):
        return False
    for d in data:
        if d.get("Ticker") == symbol.upper():
            date_str = d.get("Date") or d.get("TradeDate")
            if not date_str:
                return True
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
                if event_date >= cutoff:
                    return True
            except Exception:
                continue
    return False


def has_recent_price_target_news(symbol, cutoff=None):
    data = price_target_news(symbol, limit=5)
    if not isinstance(data, list):
        return False
    if cutoff is None:
        cutoff = datetime.utcnow() - timedelta(days=2)
    for item in data:
        date_str = item.get("publishedDate")
        if not date_str:
            continue
        try:
            pub_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
        except Exception:
            continue
        if pub_date >= cutoff:
            return True
    return False


def has_recent_patent_drift(symbol, cutoff):
    url = f"{QUIVER_BASE_URL}/live/patentdrift?ticker={symbol.upper()}&latest=true"
    data = _request_or_default(url)
    if not isinstance(data, list):
        return False
    for d in data:
        date_str = d.get("date") or d.get("Date")
        if not date_str:
            continue
        try:
            event_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
            if event_date >= cutoff:
                return True
        except Exception:
            continue
    return False


def has_recent_patent_momentum(symbol, cutoff):
    url = f"{QUIVER_BASE_URL}/live/patentmomentum?ticker={symbol.upper()}&latest=true"
    data = _request_or_default(url)
    if not isinstance(data, list):
        return False
    for d in data:
        date_str = d.get("date") or d.get("Date")
        if not date_str:
            continue
        try:
            event_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
            if event_date >= cutoff:
                return True
        except Exception:
            continue
    return False


def has_recent_patents(symbol, cutoff):
    date_from = cutoff.strftime("%Y%m%d")
    date_to = datetime.utcnow().strftime("%Y%m%d")
    url = f"{QUIVER_BASE_URL}/live/allpatents?ticker={symbol.upper()}&date_from={date_from}&date_to={date_to}"
    data = _request_or_default(url)
    return isinstance(data, list) and len(data) > 0

def is_trending_on_twitter(symbol, cutoff=None):
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint(
        "live_twitter", f"{QUIVER_BASE_URL}/live/twitter", ttl
    )
    if not isinstance(data, list):
        return False
    if cutoff is None:
        cutoff = datetime.utcnow() - timedelta(days=2)
    for d in data:
        if d.get("Ticker") != symbol.upper():
            continue
        date_str = d.get("Date") or d.get("date")
        if date_str:
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
            except Exception:
                continue
            if event_date < cutoff:
                continue
        if isinstance(d.get("Followers"), (int, float)) and d["Followers"] >= 5000:
            return True
    return False

def has_positive_app_ratings(symbol, cutoff=None):
    ttl = _ttl_heavy()
    data = _cached_heavy_endpoint(
        "live_appratings", f"{QUIVER_BASE_URL}/live/appratings", ttl
    )
    if not isinstance(data, list):
        return False
    if cutoff is None:
        cutoff = datetime.utcnow() - timedelta(days=2)
    for d in data:
        if d.get("Ticker") != symbol.upper():
            continue
        date_str = d.get("Date") or d.get("date")
        if date_str:
            try:
                event_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
            except Exception:
                continue
            if event_date < cutoff:
                continue
        if (
            isinstance(d.get("Rating"), (int, float))
            and d["Rating"] >= 4.0
            and isinstance(d.get("Count"), (int, float))
            and d["Count"] >= 10
        ):
            return True
    return False


# Indicadores compuestos
def has_political_pressure(symbol, cutoff=None):
    return get_gov_contract_signal(symbol).active or house_purchase_signal(symbol).active


def has_social_demand(symbol, cutoff=None):
    return get_wsb_signal(symbol).active or twitter_trending_signal(symbol).active

def initialize_quiver_caches():
    """
    Inicializa los datos pesados de Quiver para ser usados localmente.
    Evita llamadas repetidas a la API para datos grandes.
    """
    ttl = _ttl_heavy()
    print("🔄 Descargando datos de insiders...")
    _cached_heavy_endpoint("live_insiders", f"{QUIVER_BASE_URL}/live/insiders", ttl)
    print("🔄 Descargando datos de contratos gubernamentales...")
    _cached_heavy_endpoint("live_govcontracts", f"{QUIVER_BASE_URL}/live/govcontracts", ttl)
    print("🔄 Descargando datos de housetrading...")
    _cached_heavy_endpoint("live_housetrading", f"{QUIVER_BASE_URL}/live/housetrading", ttl)
    print("🔄 Descargando datos de Twitter...")
    _cached_heavy_endpoint("live_twitter", f"{QUIVER_BASE_URL}/live/twitter", ttl)
    print("🔄 Descargando datos de app ratings...")
    _cached_heavy_endpoint("live_appratings", f"{QUIVER_BASE_URL}/live/appratings", ttl)


