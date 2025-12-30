"""Isolate Quiver API ingestion so feature logic stays pure and testable."""

from __future__ import annotations

import os
import random
import time
from datetime import datetime
from typing import Optional

import requests
from dotenv import load_dotenv

import config
from signals.quiver_throttler import throttled_request
from utils.cache import get as cache_get, set as cache_set
from utils.logger import log_event, log_once


class QuiverRateLimitError(Exception):
    """Raised when the Quiver API responds with a rate limit."""


class QuiverTemporaryError(Exception):
    """Raised when a transient error should suppress further requests."""


load_dotenv()

QUIVER_API_KEY = os.getenv("QUIVER_API_KEY")
QUIVER_BASE_URL = "https://api.quiverquant.com/beta"
HEADERS = {"Authorization": f"Bearer {QUIVER_API_KEY}"}
QUIVER_TIMEOUT = int(os.getenv("QUIVER_TIMEOUT", "30"))

_ENDPOINT_SUPPRESS: dict[str, float] = {}


def _ttl_lot() -> int:
    cfg = getattr(config, "_policy", {}) or {}
    return int(((cfg.get("cache") or {}).get("lot_ttl_sec", 900)))


def _ttl_heavy() -> int:
    cfg = getattr(config, "_policy", {}) or {}
    cache_cfg = cfg.get("cache") or {}
    return int(cache_cfg.get("quiver_heavy_ttl_sec", _ttl_lot()))


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


def safe_quiver_request(url, retries=5, delay=4):
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
            if r.status_code == 429:
                last_error = QuiverRateLimitError("rate_limit")
                wait = delay * (2**i)
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
        wait = delay * (2**i)
        wait += random.uniform(0, delay)
        print(f"🔄 Reintentando en {wait}s...")
        time.sleep(wait)
    print(f"❌ Fallo final en {url}. Se devuelve None.")
    if last_error:
        raise last_error
    return None


def _request_or_default(url: str, default=None):
    try:
        return safe_quiver_request(url)
    except (QuiverRateLimitError, QuiverTemporaryError):
        return default


def fetch_live_insiders():
    ttl = _ttl_heavy()
    return _cached_heavy_endpoint("live_insiders", f"{QUIVER_BASE_URL}/live/insiders", ttl)


def fetch_live_govcontracts():
    ttl = _ttl_heavy()
    return _cached_heavy_endpoint("live_govcontracts", f"{QUIVER_BASE_URL}/live/govcontracts", ttl)


def fetch_live_housetrading():
    ttl = _ttl_heavy()
    return _cached_heavy_endpoint("live_housetrading", f"{QUIVER_BASE_URL}/live/housetrading", ttl)


def fetch_live_twitter():
    ttl = _ttl_heavy()
    return _cached_heavy_endpoint("live_twitter", f"{QUIVER_BASE_URL}/live/twitter", ttl)


def fetch_live_appratings():
    return _request_or_default(f"{QUIVER_BASE_URL}/live/appratings")


def fetch_live_appratings_cached():
    ttl = _ttl_heavy()
    return _cached_heavy_endpoint("live_appratings", f"{QUIVER_BASE_URL}/live/appratings", ttl)


def fetch_live_sec13f():
    return _request_or_default(f"{QUIVER_BASE_URL}/live/sec13f")


def fetch_live_sec13f_cached():
    ttl = _ttl_heavy()
    return _cached_heavy_endpoint("live_sec13f", f"{QUIVER_BASE_URL}/live/sec13f", ttl)


def fetch_live_sec13fchanges():
    return _request_or_default(f"{QUIVER_BASE_URL}/live/sec13fchanges")


def fetch_live_sec13fchanges_cached():
    ttl = _ttl_heavy()
    return _cached_heavy_endpoint("live_sec13fchanges", f"{QUIVER_BASE_URL}/live/sec13fchanges", ttl)


def fetch_live_senatetrading():
    return _request_or_default(f"{QUIVER_BASE_URL}/live/senatetrading")


def fetch_live_congresstrading():
    return _request_or_default(f"{QUIVER_BASE_URL}/live/congresstrading")


def fetch_live_govcontractsall():
    return _request_or_default(f"{QUIVER_BASE_URL}/live/govcontractsall")


def fetch_live_lobbying():
    return _request_or_default(f"{QUIVER_BASE_URL}/live/lobbying")


def fetch_live_offexchange():
    return _request_or_default(f"{QUIVER_BASE_URL}/live/offexchange")


def fetch_live_patentmomentum():
    return _request_or_default(f"{QUIVER_BASE_URL}/live/patentmomentum")


def fetch_live_patentdrift(symbol: str):
    return _request_or_default(
        f"{QUIVER_BASE_URL}/live/patentdrift?ticker={symbol.upper()}&latest=true"
    )


def fetch_live_patentmomentum_latest(symbol: str):
    return _request_or_default(
        f"{QUIVER_BASE_URL}/live/patentmomentum?ticker={symbol.upper()}&latest=true"
    )


def fetch_live_allpatents(symbol: str, date_from: str, date_to: str):
    url = (
        f"{QUIVER_BASE_URL}/live/allpatents?"
        f"ticker={symbol.upper()}&date_from={date_from}&date_to={date_to}"
    )
    return _request_or_default(url)


def fetch_historical_wallstreetbets(symbol: str):
    return _request_or_default(
        f"{QUIVER_BASE_URL}/historical/wallstreetbets/{symbol.upper()}"
    )


def fetch_historical_congresstrading(symbol: str):
    return _request_or_default(
        f"{QUIVER_BASE_URL}/historical/congresstrading/{symbol.upper()}"
    )


def fetch_historical_senatetrading(symbol: str):
    return _request_or_default(
        f"{QUIVER_BASE_URL}/historical/senatetrading/{symbol.upper()}"
    )


def ingest_symbol_payload(symbol: str) -> dict[str, dict[str, list[dict]]]:
    """Return raw Quiver payloads filtered to ``symbol`` without scoring."""
    sym = symbol.upper()
    insiders = [
        {
            "date": item.get("Date"),
            "transaction_code": item.get("TransactionCode"),
            "shares": item.get("Shares"),
            "price": item.get("Price"),
            "owner": item.get("Owner"),
        }
        for item in (fetch_live_insiders() or [])
        if item.get("Ticker") == sym
    ]
    gov_contracts = [
        {
            "date": item.get("Date") or item.get("AnnouncementDate"),
            "amount": item.get("Amount"),
            "agency": item.get("Agency"),
        }
        for item in (fetch_live_govcontracts() or [])
        if item.get("Ticker") == sym
    ]
    house_trades = [
        {
            "date": item.get("Date"),
            "transaction": item.get("Transaction"),
            "amount": item.get("Amount"),
        }
        for item in (fetch_live_housetrading() or [])
        if item.get("Ticker") == sym
    ]
    twitter = [
        {
            "date": item.get("Date") or item.get("date"),
            "followers": item.get("Followers"),
            "tweet": item.get("Tweet"),
        }
        for item in (fetch_live_twitter() or [])
        if item.get("Ticker") == sym
    ]
    app_ratings = [
        {
            "date": item.get("Date") or item.get("date"),
            "rating": item.get("Rating"),
            "count": item.get("Count"),
        }
        for item in (fetch_live_appratings() or [])
        if item.get("Ticker") == sym
    ]
    patent_momentum = [
        {
            "date": item.get("date") or item.get("Date"),
            "momentum": item.get("momentum"),
        }
        for item in (fetch_live_patentmomentum() or [])
        if item.get("ticker") == sym
    ]
    sec13f = [
        {"date": item.get("ReportDate") or item.get("Date"), "ticker": item.get("Ticker")}
        for item in (fetch_live_sec13f() or [])
        if item.get("Ticker") == sym
    ]
    sec13f_changes = [
        {
            "date": item.get("ReportDate") or item.get("Date"),
            "change_pct": item.get("Change_Pct"),
        }
        for item in (fetch_live_sec13fchanges() or [])
        if item.get("Ticker") == sym
    ]
    return {
        sym: {
            "insider_trades": insiders,
            "gov_contracts": gov_contracts,
            "house_trades": house_trades,
            "twitter_mentions": twitter,
            "app_ratings": app_ratings,
            "patent_momentum": patent_momentum,
            "sec13f": sec13f,
            "sec13f_changes": sec13f_changes,
        }
    }


def initialize_quiver_caches():
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
