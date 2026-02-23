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
from utils.persistent_cache import get as persist_get, set as persist_set


def _ttl_symbol() -> int:
    cfg = getattr(config, "_policy", {}) or {}
    return int(((cfg.get("cache") or {}).get("symbol_ttl_sec", 600)))


def _freshness_days() -> int:
    cfg = getattr(config, "_policy", {}) or {}
    return int(((cfg.get("signals") or {}).get("freshness_days_quiver", 7)))


def _freshness_days_gov_contracts() -> int:
    cfg = getattr(config, "_policy", {}) or {}
    return int(((cfg.get("signals") or {}).get("freshness_days_gov_contracts", 90)))


def _age_days(dt: datetime) -> float:
    now = datetime.now(timezone.utc)
    delta = now - dt
    return max(delta.total_seconds() / 86400.0, 0.0)

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


def _insider_trade_features(symbol: str, freshness_days: int) -> tuple[dict[str, float | int], list[float]]:
    data = quiver_ingest.fetch_live_insiders()
    buys = 0
    sells = 0
    ages: list[float] = []
    if isinstance(data, list):
        for item in data:
            if item.get("Ticker") != symbol.upper():
                continue
            dt = _parse_dt(item.get("Date"))
            if dt is not None:
                age = _age_days(dt)
                if age > freshness_days:
                    continue
                ages.append(age)
            code = item.get("TransactionCode")
            if code == "P":
                buys += 1
            elif code == "S":
                sells += 1
    return (
        {
            "buy_count": buys,
            "sell_count": sells,
        },
        ages,
    )


def _gov_contract_features(symbol: str, freshness_days: int) -> tuple[dict[str, float | int], list[float]]:
    data = quiver_ingest.fetch_live_govcontracts()
    total_amount = 0.0
    count = 0
    ages: list[float] = []
    if isinstance(data, list):
        for item in data:
            if item.get("Ticker") != symbol.upper():
                continue
            dt = _parse_dt(item.get("Date") or item.get("AnnouncementDate"))
            if dt is not None:
                age = _age_days(dt)
                if age > freshness_days:
                    continue
                ages.append(age)
            try:
                amt = float(str(item.get("Amount", "0")).replace("$", "").replace(",", ""))
            except Exception:
                amt = 0.0
            total_amount += amt
            count += 1
    return ({"total_amount": total_amount, "count": count}, ages)


def _patent_momentum_features(symbol: str, freshness_days: int) -> tuple[dict[str, float], list[float]]:
    data = quiver_ingest.fetch_live_patentmomentum_cached()
    latest_value = 0.0
    ages: list[float] = []
    if isinstance(data, list):
        items = [item for item in data if item.get("ticker") == symbol.upper()]
        latest = _latest_item(items, ("date", "Date"))
        if latest and isinstance(latest.get("momentum"), (int, float)):
            dt = _parse_dt(latest.get("date") or latest.get("Date"))
            if dt is None:
                latest_value = float(latest.get("momentum"))
            else:
                age = _age_days(dt)
                if age <= freshness_days:
                    latest_value = float(latest.get("momentum"))
                    ages.append(age)
    return ({"latest_momentum": latest_value}, ages)


def _wsb_features(symbol: str, freshness_days: int) -> tuple[dict[str, float], list[float]]:
    data = quiver_ingest.fetch_historical_wallstreetbets(symbol)
    max_mentions = 0.0
    ages: list[float] = []
    if isinstance(data, list):
        for item in data[-5:]:
            mentions = item.get("Mentions")
            if isinstance(mentions, (int, float)):
                dt = _parse_dt(item.get("Date") or item.get("date"))
                if dt is None:
                    max_mentions = max(max_mentions, float(mentions))
                    continue
                age = _age_days(dt)
                if age <= freshness_days:
                    max_mentions = max(max_mentions, float(mentions))
                    ages.append(age)
    return ({"recent_max_mentions": max_mentions}, ages)


def _sec13f_features(symbol: str, freshness_days: int) -> tuple[dict[str, float], list[float]]:
    data = quiver_ingest.fetch_live_sec13f_cached()
    count = 0
    ages: list[float] = []
    if isinstance(data, list):
        for item in data:
            if item.get("Ticker") != symbol.upper():
                continue
            dt = _parse_dt(item.get("ReportDate") or item.get("Date"))
            if dt is not None:
                age = _age_days(dt)
                if age > freshness_days:
                    continue
                ages.append(age)
            count += 1
    return ({"count": float(count)}, ages)


def _sec13f_change_features(symbol: str, freshness_days: int) -> tuple[dict[str, float], list[float]]:
    data = quiver_ingest.fetch_live_sec13fchanges_cached()
    latest_change = 0.0
    ages: list[float] = []
    if isinstance(data, list):
        items = [item for item in data if item.get("Ticker") == symbol.upper()]
        latest = _latest_item(items, ("ReportDate", "Date"))
        if latest and isinstance(latest.get("Change_Pct"), (int, float)):
            dt = _parse_dt(latest.get("ReportDate") or latest.get("Date"))
            if dt is None:
                latest_change = float(latest.get("Change_Pct"))
            else:
                age = _age_days(dt)
                if age <= freshness_days:
                    latest_change = float(latest.get("Change_Pct"))
                    ages.append(age)
    return ({"latest_change_pct": latest_change}, ages)


def _house_purchase_features(symbol: str, freshness_days: int) -> tuple[dict[str, float], list[float]]:
    data = quiver_ingest.fetch_live_housetrading()
    count = 0
    ages: list[float] = []
    if isinstance(data, list):
        for item in data:
            if item.get("Ticker") != symbol.upper() or item.get("Transaction") != "Purchase":
                continue
            dt = _parse_dt(item.get("Date"))
            if dt is not None:
                age = _age_days(dt)
                if age > freshness_days:
                    continue
                ages.append(age)
            count += 1
    return ({"count": float(count)}, ages)


def _twitter_features(symbol: str, freshness_days: int) -> tuple[dict[str, float], list[float]]:
    data = quiver_ingest.fetch_live_twitter()
    latest_followers = 0.0
    ages: list[float] = []
    if isinstance(data, list):
        items = [item for item in data if item.get("Ticker") == symbol.upper()]
        latest = _latest_item(items, ("Date", "date"))
        if latest and isinstance(latest.get("Followers"), (int, float)):
            dt = _parse_dt(latest.get("Date") or latest.get("date"))
            if dt is None:
                latest_followers = float(latest.get("Followers"))
            else:
                age = _age_days(dt)
                if age <= freshness_days:
                    latest_followers = float(latest.get("Followers"))
                    ages.append(age)
    return ({"latest_followers": latest_followers}, ages)


def _senate_purchase_features(symbol: str, freshness_days: int) -> tuple[dict[str, float], list[float]]:
    data = quiver_ingest.fetch_live_senatetrading_cached()
    count = 0
    ages: list[float] = []
    if isinstance(data, list):
        for item in data:
            if item.get("Ticker") != symbol.upper():
                continue
            transaction = (item.get("Transaction") or "").strip().lower()
            if transaction not in ("purchase", "buy"):
                continue
            dt = _parse_dt(item.get("Date") or item.get("TransactionDate"))
            if dt is not None:
                age = _age_days(dt)
                if age > freshness_days:
                    continue
                ages.append(age)
            count += 1
    return ({"count": float(count)}, ages)


def _congress_purchase_features(symbol: str, freshness_days: int) -> tuple[dict[str, float], list[float]]:
    """Congress live endpoint uses TransactionDate (not Date) for the trade date."""
    data = quiver_ingest.fetch_live_congresstrading_cached()
    count = 0
    ages: list[float] = []
    if isinstance(data, list):
        for item in data:
            if item.get("Ticker") != symbol.upper():
                continue
            transaction = (item.get("Transaction") or "").strip().lower()
            if transaction not in ("purchase", "buy"):
                continue
            dt = _parse_dt(item.get("TransactionDate") or item.get("Date"))
            if dt is not None:
                age = _age_days(dt)
                if age > freshness_days:
                    continue
                ages.append(age)
            count += 1
    return ({"count": float(count)}, ages)


def _app_ratings_features(symbol: str, freshness_days: int) -> tuple[dict[str, float], list[float]]:
    data = quiver_ingest.fetch_live_appratings_cached()
    latest_rating = 0.0
    latest_count = 0.0
    ages: list[float] = []
    if isinstance(data, list):
        items = [item for item in data if item.get("Ticker") == symbol.upper()]
        latest = _latest_item(items, ("Date", "date"))
        if latest:
            rating = latest.get("Rating")
            count = latest.get("Count")
            dt = _parse_dt(latest.get("Date") or latest.get("date"))
            if dt is None or _age_days(dt) <= freshness_days:
                if isinstance(rating, (int, float)):
                    latest_rating = float(rating)
                if isinstance(count, (int, float)):
                    latest_count = float(count)
                if dt is not None:
                    ages.append(_age_days(dt))
    return ({"latest_rating": latest_rating, "latest_count": latest_count}, ages)


def get_quiver_features(symbol: str) -> dict[str, float | int]:
    """Return numeric Quiver features without scoring or thresholds."""
    freshness_days = _freshness_days()
    freshness_days_gov = _freshness_days_gov_contracts()
    ages: list[float] = []
    insider, insider_ages = _insider_trade_features(symbol, freshness_days)
    gov, gov_ages = _gov_contract_features(symbol, freshness_days_gov)
    patent, patent_ages = _patent_momentum_features(symbol, freshness_days)
    wsb, wsb_ages = _wsb_features(symbol, freshness_days)
    sec13f, sec13f_ages = _sec13f_features(symbol, freshness_days)
    sec13f_changes, sec13f_change_ages = _sec13f_change_features(symbol, freshness_days)
    house, house_ages = _house_purchase_features(symbol, freshness_days)
    senate, senate_ages = _senate_purchase_features(symbol, freshness_days)
    congress, congress_ages = _congress_purchase_features(symbol, freshness_days)
    twitter, twitter_ages = _twitter_features(symbol, freshness_days)
    app_ratings, app_ratings_ages = _app_ratings_features(symbol, freshness_days)
    ages.extend(
        insider_ages
        + gov_ages
        + patent_ages
        + wsb_ages
        + sec13f_ages
        + sec13f_change_ages
        + house_ages
        + senate_ages
        + congress_ages
        + twitter_ages
        + app_ratings_ages
    )
    age_min = min(ages) if ages else 0.0

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
        "quiver_senate_purchase_count": senate["count"],
        "quiver_congress_purchase_count": congress["count"],
        "quiver_twitter_latest_followers": twitter["latest_followers"],
        "quiver_app_rating_latest": app_ratings["latest_rating"],
        "quiver_app_rating_latest_count": app_ratings["latest_count"],
        "quiver_signal_age_days_min": age_min,
    }


def _has_quiver_signal(features: dict[str, float | int]) -> bool:
    for key, value in features.items():
        if key == "quiver_signal_age_days_min":
            continue
        try:
            if float(value) != 0.0:
                return True
        except Exception:
            continue
    return False


def fetch_quiver_signals(symbol: str, fallback_symbol: str | None = None) -> dict[str, float | int]:
    """Cached access to Quiver feature snapshots."""
    if not config.ENABLE_QUIVER:
        return {}
    ttl = _ttl_symbol()
    k = f"Q_SIG:{symbol.upper()}"
    v = cache_get(k, ttl)
    if v is not None:
        return v
    v = persist_get(k, ttl)
    if v is not None:
        cache_set(k, v)
        return v
    res = get_quiver_features(symbol)
    if fallback_symbol and fallback_symbol.upper() != symbol.upper() and not _has_quiver_signal(res):
        fallback_key = f"Q_SIG:{fallback_symbol.upper()}"
        fallback_cached = cache_get(fallback_key, ttl) or persist_get(fallback_key, ttl)
        if fallback_cached is not None:
            res = fallback_cached
        else:
            fallback_res = get_quiver_features(fallback_symbol)
            if _has_quiver_signal(fallback_res):
                res = fallback_res
            cache_set(fallback_key, fallback_res)
            persist_set(fallback_key, fallback_res)
    cache_set(k, res)
    persist_set(k, res)
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
