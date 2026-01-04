"""Signal reader and scorer for long-only equity trades."""

from __future__ import annotations

import csv
import os
from typing import Iterable, List, Tuple

import config
from signals.features import get_symbol_features
from utils.logger import log_event
from utils.symbols import detect_asset_class

SignalTuple = Tuple[str, float, float, float, float | None, float | None]


QUIVER_FEATURE_WEIGHTS = {
    "quiver_insider_buy_count": 1.0,
    "quiver_insider_sell_count": -1.0,
    "quiver_gov_contract_total_amount": 0.000001,
    "quiver_gov_contract_count": 0.5,
    "quiver_patent_momentum_latest": 1.0,
    "quiver_wsb_recent_max_mentions": 0.1,
    "quiver_sec13f_count": 0.2,
    "quiver_sec13f_change_latest_pct": 0.1,
    "quiver_house_purchase_count": 0.5,
    "quiver_twitter_latest_followers": 0.0001,
    "quiver_app_rating_latest": 0.5,
    "quiver_app_rating_latest_count": 0.05,
}

FMP_FEATURE_WEIGHTS = {
    # FMP booster only — intentionally minimal
    "fmp_grade_score": 0.2,
    "fmp_rating_overall_score": 0.05,
}

YAHOO_FEATURE_WEIGHTS = {
    "yahoo_weekly_change_pct": 0.05,
    "yahoo_price_change_24h_pct": 0.02,
    "yahoo_trend_positive": 0.1,
}

FEATURE_WEIGHTS: dict[str, float] = {}
if config.ENABLE_QUIVER:
    FEATURE_WEIGHTS.update(QUIVER_FEATURE_WEIGHTS)
if config.ENABLE_FMP:
    FEATURE_WEIGHTS.update(FMP_FEATURE_WEIGHTS)
if config.ENABLE_YAHOO:
    FEATURE_WEIGHTS.update(YAHOO_FEATURE_WEIGHTS)


def _load_symbols(path: str = "data/symbols.csv") -> List[str]:
    symbols: List[str] = []
    if not os.path.exists(path):
        log_event(f"SCAN symbols.csv missing path={path}", event="SCAN")
        return symbols
    try:
        with open(path, newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                raw_symbol = (row.get("Symbol") or "").strip().upper()
                if not raw_symbol:
                    continue
                if detect_asset_class(raw_symbol) != "equity":
                    continue
                name = (row.get("Name") or "").upper()
                if "ETF" in name:
                    continue
                symbols.append(raw_symbol)
    except Exception as exc:
        log_event(f"SCAN symbols.csv read failed error={exc}", event="SCAN")
    return symbols


def _signal_threshold() -> float:
    return float((getattr(config, "_policy", {}) or {}).get("signals", {}).get("approval_threshold", 7.0))


def _score_from_features(features: dict[str, float]) -> float:
    """Linear score computed from numeric features only."""
    score = 0.0
    for key, weight in FEATURE_WEIGHTS.items():
        score += weight * float(features.get(key, 0.0))
    return score


def get_top_signals(
    *,
    max_symbols: int = 30,
    exclude: Iterable[str] | None = None,
) -> List[SignalTuple]:
    """Return a list of approved signals for the current scan cycle."""

    log_event(
        "PROVIDERS: "
        f"QUIVER={'ON' if config.ENABLE_QUIVER else 'OFF'}, "
        f"YAHOO={'ON' if config.ENABLE_YAHOO else 'OFF'}, "
        f"FMP={'ON' if config.ENABLE_FMP else 'OFF'}",
        event="SCAN",
    )

    exclude_set = {s.upper() for s in (exclude or [])}
    symbols = _load_symbols()
    if not symbols:
        log_event("SCAN no symbols to evaluate", event="SCAN")
        return []

    evaluated: List[str] = []
    approved: List[SignalTuple] = []
    rejected: List[str] = []
    threshold = _signal_threshold()

    for symbol in symbols:
        if len(evaluated) >= max_symbols:
            break
        if symbol in exclude_set:
            rejected.append(f"{symbol}:excluded")
            continue

        evaluated.append(symbol)

        features = get_symbol_features(symbol)
        total_score = _score_from_features(features)
        approved_flag = total_score >= threshold

        log_event(
            (
                f"APPROVAL {symbol}: features={features} "
                f"score={total_score:.2f} threshold={threshold:.2f} "
                f"approved={approved_flag}"
            ),
            event="APPROVAL",
        )

        if not approved_flag:
            rejected.append(f"{symbol}:below_threshold")
            continue

        current_price = features.get("yahoo_current_price")
        atr = features.get("yahoo_atr")

        approved.append(
            (
                symbol,
                float(total_score),
                0.0,
                0.0,
                float(current_price) if current_price is not None else None,
                float(atr) if atr is not None else None,
            )
        )

    log_event(
        f"SCAN evaluated={evaluated}",
        event="SCAN",
    )
    if rejected:
        log_event(f"SCAN rejected={rejected}", event="SCAN")
    log_event(
        f"SCAN approved={[s[0] for s in approved]}",
        event="SCAN",
    )

    return approved
