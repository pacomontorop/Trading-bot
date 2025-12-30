# quiver_utils.py

"""Quiver numeric feature helpers.

This module strips out recency gates and boolean decisions. It only returns
raw numeric features so scoring can live in one place.
"""

from __future__ import annotations

from datetime import datetime, timezone

import config
from signals import quiver_ingest
from utils.cache import get as cache_get, set as cache_set


def _ttl_symbol() -> int:
    cfg = getattr(config, "_policy", {}) or {}
    return int(((cfg.get("cache") or {}).get("symbol_ttl_sec", 600)))


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _latest_item(items, date_keys: tuple[str, ...]):
    latest = None
    latest_dt = None
    for item in items:
        dt = None
        for key in date_keys:
            dt = _parse_dt(item.get(key))
            if dt:
                break
        if dt and (latest_dt is None or dt > latest_dt):
            latest = item
            latest_dt = dt
    return latest


def _insider_trade_features(symbol: str) -> dict[str, float | int]:
    data = quiver_ingest.fetch_live_insiders()
    buys = 0
    sells = 0
    if isinstance(data, list):
        for item in data:
            if item.get("Ticker") != symbol.upper():
                continue
            code = item.get("TransactionCode")
            if code == "P":
                buys += 1
            elif code == "S":
                sells += 1
    return {
        "buy_count": buys,
        "sell_count": sells,
    }


def _gov_contract_features(symbol: str) -> dict[str, float | int]:
    data = quiver_ingest.fetch_live_govcontracts()
    total_amount = 0.0
    count = 0
    if isinstance(data, list):
        for item in data:
            if item.get("Ticker") != symbol.upper():
                continue
            try:
                amt = float(str(item.get("Amount", "0")).replace("$", "").replace(",", ""))
            except Exception:
                amt = 0.0
            total_amount += amt
            count += 1
    return {"total_amount": total_amount, "count": count}


def _patent_momentum_features(symbol: str) -> dict[str, float]:
    data = quiver_ingest.fetch_live_patentmomentum()
    latest_value = 0.0
    if isinstance(data, list):
        items = [item for item in data if item.get("ticker") == symbol.upper()]
        latest = _latest_item(items, ("date", "Date"))
        if latest and isinstance(latest.get("momentum"), (int, float)):
            latest_value = float(latest.get("momentum"))
    return {"latest_momentum": latest_value}


def _wsb_features(symbol: str) -> dict[str, float]:
    data = quiver_ingest.fetch_historical_wallstreetbets(symbol)
    max_mentions = 0.0
    if isinstance(data, list):
        for item in data[-5:]:
            mentions = item.get("Mentions")
            if isinstance(mentions, (int, float)):
                max_mentions = max(max_mentions, float(mentions))
    return {"recent_max_mentions": max_mentions}


def _sec13f_features(symbol: str) -> dict[str, float]:
    data = quiver_ingest.fetch_live_sec13f()
    count = 0
    if isinstance(data, list):
        count = sum(1 for item in data if item.get("Ticker") == symbol.upper())
    return {"count": float(count)}


def _sec13f_change_features(symbol: str) -> dict[str, float]:
    data = quiver_ingest.fetch_live_sec13fchanges()
    latest_change = 0.0
    if isinstance(data, list):
        items = [item for item in data if item.get("Ticker") == symbol.upper()]
        latest = _latest_item(items, ("ReportDate", "Date"))
        if latest and isinstance(latest.get("Change_Pct"), (int, float)):
            latest_change = float(latest.get("Change_Pct"))
    return {"latest_change_pct": latest_change}


def _house_purchase_features(symbol: str) -> dict[str, float]:
    data = quiver_ingest.fetch_live_housetrading()
    count = 0
    if isinstance(data, list):
        count = sum(
            1
            for item in data
            if item.get("Ticker") == symbol.upper() and item.get("Transaction") == "Purchase"
        )
    return {"count": float(count)}


def _twitter_features(symbol: str) -> dict[str, float]:
    data = quiver_ingest.fetch_live_twitter()
    latest_followers = 0.0
    if isinstance(data, list):
        items = [item for item in data if item.get("Ticker") == symbol.upper()]
        latest = _latest_item(items, ("Date", "date"))
        if latest and isinstance(latest.get("Followers"), (int, float)):
            latest_followers = float(latest.get("Followers"))
    return {"latest_followers": latest_followers}


def _app_ratings_features(symbol: str) -> dict[str, float]:
    data = quiver_ingest.fetch_live_appratings()
    latest_rating = 0.0
    latest_count = 0.0
    if isinstance(data, list):
        items = [item for item in data if item.get("Ticker") == symbol.upper()]
        latest = _latest_item(items, ("Date", "date"))
        if latest:
            rating = latest.get("Rating")
            count = latest.get("Count")
            if isinstance(rating, (int, float)):
                latest_rating = float(rating)
            if isinstance(count, (int, float)):
                latest_count = float(count)
    return {
        "latest_rating": latest_rating,
        "latest_count": latest_count,
    }


def get_quiver_features(symbol: str) -> dict[str, float | int]:
    """Return numeric Quiver features without scoring or thresholds."""
    insider = _insider_trade_features(symbol)
    gov = _gov_contract_features(symbol)
    patent = _patent_momentum_features(symbol)
    wsb = _wsb_features(symbol)
    sec13f = _sec13f_features(symbol)
    sec13f_changes = _sec13f_change_features(symbol)
    house = _house_purchase_features(symbol)
    twitter = _twitter_features(symbol)
    app_ratings = _app_ratings_features(symbol)

    return {
        "quiver_insider_buy_count": insider["buy_count"],
        "quiver_insider_sell_count": insider["sell_count"],
        "quiver_gov_contract_total_amount": gov["total_amount"],
        "quiver_gov_contract_count": gov["count"],
        "quiver_patent_momentum_latest": patent["latest_momentum"],
        "quiver_wsb_recent_max_mentions": wsb["recent_max_mentions"],
        "quiver_sec13f_count": sec13f["count"],
        "quiver_sec13f_change_latest_pct": sec13f_changes["latest_change_pct"],
        "quiver_house_purchase_count": house["count"],
        "quiver_twitter_latest_followers": twitter["latest_followers"],
        "quiver_app_rating_latest": app_ratings["latest_rating"],
        "quiver_app_rating_latest_count": app_ratings["latest_count"],
    }


def fetch_quiver_signals(symbol: str) -> dict[str, float | int]:
    """Cached access to Quiver feature snapshots."""
    ttl = _ttl_symbol()
    k = f"Q_SIG:{symbol.upper()}"
    v = cache_get(k, ttl)
    if v is not None:
        return v
    res = get_quiver_features(symbol)
    cache_set(k, res)
    return res


def get_all_quiver_signals(symbol: str) -> dict[str, float | int]:
    """Return Quiver features for compatibility with legacy callers."""
    return fetch_quiver_signals(symbol)


def is_approved_by_quiver(symbol: str) -> dict:
    """Return Quiver feature payload without making approval decisions."""
    return {"features": fetch_quiver_signals(symbol)}


def evaluate_quiver_signals(signals, symbol: str = ""):
    """Log Quiver feature snapshots for debugging."""
    print(f"\n🧪 Evaluando señales Quiver para {symbol}...")
    for key, value in (signals or {}).items():
        print(f"   • {key}: {value}")
    return {"features": signals or {}}


def initialize_quiver_caches():
    """Inicializa los datos pesados de Quiver para ser usados localmente."""
    quiver_ingest.initialize_quiver_caches()
