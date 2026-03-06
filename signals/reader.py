"""Signal reader and scorer for long-only equity trades."""

from __future__ import annotations

import json
import os
import random
import datetime as _dt
from collections import Counter
from datetime import timezone
from typing import Iterable, List, Tuple

import config
from core import risk_manager
from signals.features import get_symbol_features, compute_rsi_from_hist
from signals.scoring import fetch_yahoo_snapshot
from utils.logger import log_event
from utils.universe import load_universe

SignalTuple = Tuple[str, float, float, float | None, float | None, dict]


QUIVER_FEATURE_WEIGHTS = {
    # Insider activity — net count is the primary signal (buys - sells)
    "quiver_insider_net_count": 1.5,        # net insider buys: strongest long signal; max 5×1.5 = 7.5 pts
    "quiver_insider_buy_count": 0.5,        # raw buys still contribute; max 5×0.5 = 2.5 pts
    "quiver_insider_sell_count": -1.0,      # raw sells penalize; max -5×1.0 = -5 pts
    # Government contracts
    "quiver_gov_contract_total_amount": 0.000001,  # capped at $5M → max 5 pts
    "quiver_gov_contract_count": 0.5,              # max 5×0.5 = 2.5 pts
    # Congressional purchases (data from Quiver house trading)
    "quiver_house_purchase_count": 1.0,     # congress members buying = meaningful; max 5×1.0 = 5 pts
    # Innovation / IP
    "quiver_patent_momentum_latest": 1.0,   # max 5×1.0 = 5 pts
    # Institutional interest (13F filings)
    "quiver_sec13f_count": 0.3,             # max 5×0.3 = 1.5 pts (minor confirmation)
    "quiver_sec13f_change_latest_pct": 0.15,# max 20×0.15 = 3 pts
    # Retail sentiment — noisy, kept low
    "quiver_wsb_recent_max_mentions": 0.01, # reduced: max 500×0.01 = 5 pts (was 15)
    # Social / app signals — minor
    "quiver_twitter_latest_followers": 0.00005,       # max ~5M×0.00005 = 250 → capped at 0.5 pts
    "quiver_app_rating_latest": 0.2,                  # max 5.0×0.2 = 1.0 pts
    "quiver_app_rating_latest_count": 0.02,           # capped at 100 → max 2 pts (was uncapped = 100+ pts)
}

_FEATURE_CAPS = {
    "quiver_gov_contract_total_amount": 5_000_000,   # $5M cap → max 5 pts (was $200M → 200 pts)
    "quiver_wsb_recent_max_mentions": 500,
    "quiver_insider_buy_count": 5,
    "quiver_insider_net_count": 5,
    "quiver_gov_contract_count": 5,
    "quiver_house_purchase_count": 5,
    "quiver_sec13f_change_latest_pct": 20,
    "quiver_patent_momentum_latest": 5,
    "quiver_app_rating_latest_count": 100,            # cap: 100 reviews max (added — was uncapped)
    "quiver_twitter_latest_followers": 10_000_000,    # cap: 10M followers → 0.5 pts max
}


def _policy_section(name: str) -> dict:
    return (getattr(config, "_policy", {}) or {}).get(name, {}) or {}


def _strict_gates_enabled() -> bool:
    value = os.getenv("STRICT_GATES", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _signal_cfg() -> dict:
    return _policy_section("signals")


def _signal_threshold() -> float:
    return float(_signal_cfg().get("approval_threshold", 0))


def _market_cfg() -> dict:
    return _policy_section("market")


def _yahoo_gate_cfg() -> dict:
    cfg = _policy_section("yahoo_gate").copy()
    if _strict_gates_enabled():
        cfg["min_avg_volume_7d"] = 1_000_000
        cfg["require_trend_positive"] = True
    return cfg


def _quiver_gate_cfg() -> dict:
    cfg = _policy_section("quiver_gate").copy()
    # Default to enabled, but auto-disable the gate when every threshold is zero.
    if _strict_gates_enabled():
        cfg["gov_contract_min_total_amount"] = 1_000_000
        cfg["gov_contract_min_count"] = 1
        cfg["patent_momentum_min"] = 1.0
        cfg["sec13f_count_min"] = 1
        cfg["sec13f_change_min_pct"] = 0.5
    return cfg


def _universe_cfg() -> dict:
    return _policy_section("universe")


def _technicals_cfg() -> dict:
    return _policy_section("technicals")


def _rsi_gate_reasons(rsi: float | None, cfg: dict) -> list[str]:
    """Return rejection reasons based on RSI bounds. Empty list = pass."""
    reasons: list[str] = []
    min_rsi = float(cfg.get("min_rsi", 0))
    max_rsi = float(cfg.get("max_rsi", 100))
    require_rsi = bool(cfg.get("require_rsi", False))
    if rsi is None or rsi == 0.0:
        if require_rsi:
            reasons.append("rsi_missing")
        return reasons
    if min_rsi > 0 and rsi < min_rsi:
        reasons.append("rsi_below_min")
    if max_rsi < 100 and rsi > max_rsi:
        reasons.append("rsi_above_max")
    return reasons


def _daily_shuffled_universe(universe: list[dict]) -> list[dict]:
    """Shuffle the universe with a daily seed so every scan cycle of the same
    day sees the same order (reproducible) but different days rotate coverage."""
    today = _dt.date.today().isoformat()
    rng = random.Random(today)
    shuffled = universe.copy()
    rng.shuffle(shuffled)
    return shuffled


# ---------------------------------------------------------------------------
# Intra-day rotation state — advances offset each cycle so every symbol in
# the universe is evaluated at least once per trading day.
# ---------------------------------------------------------------------------
_rot_date: str = ""
_rot_universe: list[dict] = []
_rot_offset: int = 0


def _cycle_batch(batch_size: int) -> list[dict]:
    """Return the next *batch_size* symbols from the daily-shuffled universe.

    Wraps around at end-of-universe so no symbols are permanently skipped.
    State resets automatically at midnight (new date = new shuffle).
    """
    global _rot_date, _rot_universe, _rot_offset

    today = _dt.date.today().isoformat()
    if _rot_date != today:
        raw = _load_universe()
        _rot_universe = _daily_shuffled_universe(raw)
        _rot_date = today
        _rot_offset = 0

    total = len(_rot_universe)
    if total == 0:
        return []

    start = _rot_offset % total
    end = start + batch_size
    if end <= total:
        batch = _rot_universe[start:end]
    else:
        batch = _rot_universe[start:] + _rot_universe[: end - total]

    _rot_offset = (start + batch_size) % total
    log_event(
        f"SCAN rotation offset={start}->{_rot_offset} total_universe={total} batch={len(batch)}",
        event="SCAN",
    )
    return batch


def _quiver_gate_disabled(cfg: dict | None = None) -> bool:
    cfg = cfg or _quiver_gate_cfg()
    enabled = bool(cfg.get("enabled", True))
    thresholds = [
        float(cfg.get("insider_buy_min_count_lookback", 0)),
        float(cfg.get("gov_contract_min_total_amount", 0)),
        float(cfg.get("gov_contract_min_count", 0)),
        float(cfg.get("patent_momentum_min", 0)),
        float(cfg.get("sec13f_count_min", 0)),
        float(cfg.get("sec13f_change_min_pct", 0)),
    ]
    any_threshold = any(value > 0 for value in thresholds)
    return (not config.ENABLE_QUIVER) or (not enabled) or (not any_threshold)


def _normalize_feature_value(key: str, value: float) -> float:
    if value is None:
        return 0.0
    numeric = float(value)
    cap = _FEATURE_CAPS.get(key)
    if cap is not None:
        numeric = min(numeric, float(cap))
    return numeric


def _score_from_features(features: dict[str, float]) -> tuple[float, float]:
    """Simple score computed from Quiver numeric features only."""
    score = 0.0
    quiver_score = 0.0
    for key, weight in QUIVER_FEATURE_WEIGHTS.items():
        value = _normalize_feature_value(key, float(features.get(key, 0.0)))
        contribution = weight * value
        score += contribution
        quiver_score += contribution
    return score, quiver_score


def _compact_features(features: dict[str, float]) -> dict[str, float]:
    compact = {k: v for k, v in features.items() if k.startswith(("quiver_", "yahoo_"))}
    trimmed = {k: v for k, v in compact.items() if v not in (0, 0.0, None)}
    return dict(list(trimmed.items())[:8])


def _fetch_yahoo_snapshot(symbol: str, yahoo_symbol: str) -> tuple[tuple, object | None, dict]:
    if not config.ENABLE_YAHOO:
        return (None, None, None, None, None, None, None, None), None, {
            "status": "disabled",
            "used_symbol": yahoo_symbol,
            "fallback_used": False,
        }
    snapshot, hist = fetch_yahoo_snapshot(
        symbol,
        yahoo_symbol=yahoo_symbol,
        fallback_symbol=symbol if yahoo_symbol != symbol else None,
        return_history=True,
    )
    return snapshot.data, hist, {
        "status": snapshot.status,
        "used_symbol": snapshot.used_symbol,
        "fallback_used": snapshot.fallback_used,
    }


def _yahoo_history_reasons(hist) -> list[str]:
    reasons: list[str] = []
    freshness_days = int(_signal_cfg().get("freshness_days_yahoo_prices", 2))
    if hist is None or hist.empty:
        reasons.append("yahoo_history_missing")
    else:
        last_dt = hist.index[-1]
        if isinstance(last_dt, _dt.datetime):
            last_dt = last_dt.tz_localize(timezone.utc) if last_dt.tzinfo is None else last_dt
            age_days = (_dt.datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
            if age_days > freshness_days:
                reasons.append("yahoo_stale")
    return reasons


def _yahoo_basic_price_reasons(current_price: float | None, min_price: float, max_price: float) -> list[str]:
    reasons: list[str] = []
    if not current_price or current_price <= 0:
        reasons.append("invalid_price")
        return reasons
    if min_price and current_price < min_price:
        reasons.append("price_below_min")
    if max_price != float("inf") and current_price > max_price:
        reasons.append("price_above_max")
    return reasons


def _yahoo_gate_reasons(
    *,
    snapshot_data: tuple,
    min_market_cap: float,
    min_avg_volume: float,
    max_atr_pct: float,
    require_trend: bool,
) -> list[str]:
    reasons: list[str] = []
    (
        market_cap,
        volume,
        weekly_change,
        trend_positive,
        price_change_24h,
        volume_7d,
        current_price,
        atr,
    ) = snapshot_data
    if min_market_cap and (market_cap or 0) < min_market_cap:
        reasons.append("market_cap_low")
    if min_avg_volume and (volume_7d or 0) < min_avg_volume:
        reasons.append("volume_low")
    if current_price and atr:
        atr_pct = (float(atr) / float(current_price)) * 100.0
        if atr_pct > max_atr_pct:
            reasons.append("atr_pct_high")
    if require_trend and not trend_positive:
        reasons.append("trend_negative")
    return reasons


def _quiver_fast_lane_summary(features: dict[str, float], cfg: dict) -> tuple[bool, list[str], dict]:
    insider_min = float(cfg.get("insider_buy_strong_min_count_7d", 2))
    gov_min = float(cfg.get("gov_contract_strong_min_total_30d", 1_000_000))
    patent_min = float(cfg.get("patent_momentum_min_strong", 90))
    insider_buys = float(features.get("quiver_insider_buy_count", 0))
    gov_total = float(features.get("quiver_gov_contract_total_amount", 0))
    patent_momentum = float(features.get("quiver_patent_momentum_latest", 0))
    reasons: list[str] = []
    if insider_buys >= insider_min:
        reasons.append("insider_buys")
    if gov_total >= gov_min:
        reasons.append("gov_contracts")
    if patent_momentum >= patent_min:
        reasons.append("patent_momentum")
    strong = bool(reasons)
    summary = {
        "insider_buys_7d": insider_buys,
        "gov_contract_total_30d": gov_total,
        "patent_momentum": patent_momentum,
        "strong_signal_bool": strong,
        "strong_reason": reasons,
    }
    return strong, reasons, summary


def gate_market_conditions() -> tuple[bool, list[str], dict]:
    reasons: list[str] = []
    cfg = _market_cfg()
    if cfg.get("global_kill_switch"):
        reasons.append("global_kill_switch")
    return not reasons, reasons, {}


def gate_quiver_minimum(features: dict[str, float]) -> tuple[bool, list[str]]:
    cfg = _quiver_gate_cfg()
    reasons: list[str] = []
    if _quiver_gate_disabled(cfg):
        return True, ["quiver_disabled"]

    checks = []
    insider_min = float(cfg.get("insider_buy_min_count_lookback", 0))
    if insider_min > 0:
        checks.append(features.get("quiver_insider_buy_count", 0) >= insider_min)
    gov_amount_min = float(cfg.get("gov_contract_min_total_amount", 0))
    if gov_amount_min > 0:
        checks.append(features.get("quiver_gov_contract_total_amount", 0) >= gov_amount_min)
    gov_count_min = float(cfg.get("gov_contract_min_count", 0))
    if gov_count_min > 0:
        checks.append(features.get("quiver_gov_contract_count", 0) >= gov_count_min)
    patent_min = float(cfg.get("patent_momentum_min", 0))
    if patent_min > 0:
        checks.append(features.get("quiver_patent_momentum_latest", 0) >= patent_min)
    sec_count_min = float(cfg.get("sec13f_count_min", 0))
    if sec_count_min > 0:
        checks.append(features.get("quiver_sec13f_count", 0) >= sec_count_min)
    sec_change_min = float(cfg.get("sec13f_change_min_pct", 0))
    if sec_change_min > 0:
        checks.append(features.get("quiver_sec13f_change_latest_pct", 0) >= sec_change_min)

    active_types = 0
    if features.get("quiver_insider_buy_count", 0) > 0:
        active_types += 1
    if features.get("quiver_gov_contract_count", 0) > 0 or features.get("quiver_gov_contract_total_amount", 0) > 0:
        active_types += 1
    if features.get("quiver_patent_momentum_latest", 0) > 0:
        active_types += 1
    if features.get("quiver_sec13f_count", 0) > 0:
        active_types += 1
    if features.get("quiver_wsb_recent_max_mentions", 0) > 0:
        active_types += 1
    if features.get("quiver_house_purchase_count", 0) > 0:
        active_types += 1

    min_types = int(cfg.get("min_active_signal_types", 1))
    if checks and not any(checks):
        reasons.append("quiver_min_signal")
    elif active_types < min_types:
        reasons.append("quiver_min_types")
    return not reasons, reasons


def _load_universe(path: str = "data/symbols.csv") -> List[dict]:
    if not os.path.exists(path):
        log_event(f"SCAN symbols.csv missing path={path}", event="SCAN")
        return []
    universe = load_universe(path)
    if not universe:
        log_event("SCAN symbols.csv read failed or empty", event="SCAN")
    return universe


def get_top_signals(
    *,
    max_symbols: int | None = None,
    exclude: Iterable[str] | None = None,
) -> List[SignalTuple]:
    """Return a list of approved signals for the current scan cycle."""

    log_event(
        "PROVIDERS: "
        f"QUIVER={'ON' if config.ENABLE_QUIVER else 'OFF'}, "
        f"YAHOO={'ON' if config.ENABLE_YAHOO else 'OFF'}",
        event="SCAN",
    )
    strict_gates = _strict_gates_enabled()
    yahoo_gate_cfg = _yahoo_gate_cfg()
    quiver_gate_cfg = _quiver_gate_cfg()
    technicals_cfg = _technicals_cfg()

    # max_symbols: caller can override; otherwise read from policy; fallback 100
    if max_symbols is None:
        max_symbols = int(_signal_cfg().get("max_symbols_per_scan", 100))

    log_event(
        "GATES effective "
        f"yahoo_gate={json.dumps(yahoo_gate_cfg, separators=(',', ':'))} "
        f"quiver_gate={json.dumps(quiver_gate_cfg, separators=(',', ':'))} "
        f"technicals_gate={json.dumps(technicals_cfg, separators=(',', ':'))} "
        f"max_symbols={max_symbols} "
        f"strict={strict_gates}",
        event="SCAN",
    )

    universe = _cycle_batch(max_symbols)
    if not universe:
        log_event("SCAN no symbols to evaluate", event="SCAN")
        return []

    sample_maps = [u["ticker_map"] for u in universe[:5]]
    log_event(f"SCAN ticker_map_sample={sample_maps}", event="SCAN")

    exclude_set = {s.upper() for s in (exclude or [])}
    evaluated: list[str] = []
    candidates: list[dict] = []
    rejected: list[str] = []
    rejection_counts: Counter[str] = Counter()
    quiver_called = 0
    yahoo_prefilter_pass = 0
    yahoo_missing = 0

    market_ok, market_reasons, market_snapshot = gate_market_conditions()

    quiver_gate_disabled = _quiver_gate_disabled(quiver_gate_cfg)

    for entry in universe:
        symbol = entry["ticker_map"]["canonical"]
        if symbol in exclude_set:
            rejected.append(f"{symbol}:excluded")
            rejection_counts["excluded"] += 1
            continue

        evaluated.append(symbol)

        yahoo_symbol = entry["ticker_map"]["yahoo"]
        quiver_symbol = entry["ticker_map"]["quiver"]
        provider_fallback_used = False

        yahoo_snapshot, yahoo_hist, yahoo_meta = _fetch_yahoo_snapshot(symbol, yahoo_symbol)
        if yahoo_meta.get("status") == "missing":
            yahoo_missing += 1
        if yahoo_meta.get("fallback_used"):
            provider_fallback_used = True

        (
            market_cap,
            volume,
            weekly_change,
            trend_positive,
            price_change_24h,
            volume_7d,
            current_price,
            atr,
        ) = yahoo_snapshot
        atr_pct = (float(atr) / float(current_price)) * 100.0 if current_price and atr else 0.0

        gate_cfg = _yahoo_gate_cfg()
        min_price = float(gate_cfg.get("min_price", 0))
        max_price = float(gate_cfg.get("max_price", float("inf")))
        price_reasons = _yahoo_basic_price_reasons(current_price, min_price, max_price)

        strict_thresholds = {
            "min_market_cap": float(gate_cfg.get("min_market_cap", 0)),
            "min_avg_volume_7d": float(gate_cfg.get("min_avg_volume_7d", 0)),
            "max_atr_pct": float(gate_cfg.get("max_atr_pct", float("inf"))),
            "min_price": min_price,
            "max_price": max_price,
            "require_trend_positive": bool(gate_cfg.get("require_trend_positive", False)),
        }

        decision_trace = {
            "symbol": symbol,
            "yahoo_symbol_used": yahoo_meta.get("used_symbol"),
            "quiver_symbol_used": quiver_symbol,
            "provider_fallback_used": provider_fallback_used,
            "yahoo_prefilter_pass": False,
            "yahoo_prefilter_reasons": [],
            "market_reasons": market_reasons,
            "quiver_fetch_status": "disabled" if not config.ENABLE_QUIVER else "pending",
            "gates_passed": {
                "market": market_ok,
                "yahoo": False,
                "quiver": False,
            },
            "quiver_gate_reasons": ["quiver_disabled"] if quiver_gate_disabled else [],
            "quiver_signal_summary": {
                "insider_buys_7d": 0,
                "gov_contract_total_30d": 0,
                "patent_momentum": 0,
                "strong_signal_bool": False,
                "strong_reason": [],
            },
            "yahoo_metrics": {
                "market_cap": market_cap,
                "avg_volume_7d": volume_7d,
                "atr_pct": atr_pct,
                "price": current_price,
            },
            "rsi": None,
            "rsi_reasons": [],
            "yahoo_mode_used": "strict_default",
            "yahoo_thresholds_used": strict_thresholds,
            "risk_check_passed": False,
            "final_decision": "REJECT",
        }

        if yahoo_meta.get("status") != "ok":
            decision_trace["yahoo_prefilter_reasons"] = ["yahoo_disabled" if yahoo_meta.get("status") == "disabled" else "yahoo_missing"]
            decision_trace["quiver_fetch_status"] = "skipped"
            rejected.append(f"{symbol}:yahoo_prefilter")
            rejection_counts["yahoo_prefilter"] += 1
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )
            continue

        if price_reasons:
            decision_trace["yahoo_prefilter_reasons"] = price_reasons
            decision_trace["quiver_fetch_status"] = "skipped"
            rejected.append(f"{symbol}:yahoo_prefilter")
            rejection_counts["yahoo_prefilter"] += 1
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )
            continue

        quiver_status = "disabled"
        if config.ENABLE_QUIVER:
            quiver_status = "ok"
            quiver_called += 1

        try:
            features = get_symbol_features(
                symbol,
                yahoo_snapshot=yahoo_snapshot,
                yahoo_symbol=yahoo_symbol,
                quiver_symbol=quiver_symbol,
                quiver_fallback_symbol=yahoo_symbol if quiver_symbol != yahoo_symbol else None,
                yahoo_hist=yahoo_hist,
            )
            decision_trace["quiver_fetch_status"] = quiver_status
        except Exception as exc:
            decision_trace["quiver_fetch_status"] = "fail"
            decision_trace["final_decision"] = "REJECT"
            strict_reasons = _yahoo_gate_reasons(
                snapshot_data=yahoo_snapshot,
                min_market_cap=strict_thresholds["min_market_cap"],
                min_avg_volume=strict_thresholds["min_avg_volume_7d"],
                max_atr_pct=strict_thresholds["max_atr_pct"],
                require_trend=strict_thresholds["require_trend_positive"],
            )
            strict_reasons.extend(_yahoo_history_reasons(yahoo_hist))
            strict_reasons.append("quiver_fetch_failed")
            decision_trace["yahoo_prefilter_reasons"] = strict_reasons
            rejected.append(f"{symbol}:feature_error")
            rejection_counts["feature_error"] += 1
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))} err={exc}",
                event="TRACE",
            )
            continue

        quiver_fast_lane_enabled = bool(quiver_gate_cfg.get("fast_lane_enabled", True))
        strong_signal, _, quiver_summary = _quiver_fast_lane_summary(features, quiver_gate_cfg)
        decision_trace["quiver_signal_summary"] = quiver_summary

        if strong_signal and quiver_fast_lane_enabled:
            relaxed_min_market_cap = float(gate_cfg.get("relaxed_min_market_cap", 300_000_000))
            relaxed_min_avg_volume = float(gate_cfg.get("relaxed_min_avg_volume_7d", 50_000))
            relaxed_max_atr_pct = float(gate_cfg.get("relaxed_max_atr_pct", 12.0))
            yahoo_reasons = _yahoo_gate_reasons(
                snapshot_data=yahoo_snapshot,
                min_market_cap=relaxed_min_market_cap,
                min_avg_volume=relaxed_min_avg_volume,
                max_atr_pct=relaxed_max_atr_pct,
                require_trend=False,
            )
            yahoo_thresholds = {
                "min_market_cap": relaxed_min_market_cap,
                "min_avg_volume_7d": relaxed_min_avg_volume,
                "max_atr_pct": relaxed_max_atr_pct,
            }
            yahoo_mode_used = "relaxed_due_to_quiver"
        else:
            strict_min_market_cap = strict_thresholds["min_market_cap"]
            strict_min_avg_volume = strict_thresholds["min_avg_volume_7d"]
            strict_max_atr_pct = strict_thresholds["max_atr_pct"]
            strict_require_trend = strict_thresholds["require_trend_positive"]
            yahoo_reasons = _yahoo_gate_reasons(
                snapshot_data=yahoo_snapshot,
                min_market_cap=strict_min_market_cap,
                min_avg_volume=strict_min_avg_volume,
                max_atr_pct=strict_max_atr_pct,
                require_trend=strict_require_trend,
            )
            yahoo_reasons.extend(_yahoo_history_reasons(yahoo_hist))
            yahoo_thresholds = strict_thresholds
            yahoo_mode_used = "strict_default"

        yahoo_ok = not yahoo_reasons
        decision_trace["yahoo_prefilter_pass"] = yahoo_ok
        decision_trace["yahoo_prefilter_reasons"] = yahoo_reasons
        decision_trace["gates_passed"]["yahoo"] = yahoo_ok
        decision_trace["yahoo_mode_used"] = yahoo_mode_used
        decision_trace["yahoo_thresholds_used"] = yahoo_thresholds

        if not yahoo_ok:
            rejected.append(f"{symbol}:yahoo_prefilter")
            rejection_counts["yahoo_prefilter"] += 1
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )
            continue

        # RSI gate — uses RSI computed from already-fetched yahoo_hist (no extra call)
        rsi_value = features.get("yahoo_rsi_14") or None
        if rsi_value == 0.0:
            rsi_value = None  # 0.0 is sentinel for "not available"
        rsi_reasons = _rsi_gate_reasons(rsi_value, technicals_cfg)
        decision_trace["rsi"] = round(rsi_value, 1) if rsi_value is not None else None
        decision_trace["rsi_reasons"] = rsi_reasons
        if rsi_reasons:
            rejected.append(f"{symbol}:rsi_gate")
            rejection_counts["rsi_gate"] += 1
            decision_trace["final_decision"] = "REJECT"
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )
            continue

        yahoo_prefilter_pass += 1

        total_score, quiver_score = _score_from_features(features)
        if strong_signal and quiver_fast_lane_enabled:
            quiver_gate_ok = True
            quiver_reasons = ["quiver_fast_lane"]
        else:
            quiver_gate_ok, quiver_reasons = gate_quiver_minimum(features)
        decision_trace["gates_passed"]["quiver"] = quiver_gate_ok
        decision_trace["quiver_gate_reasons"] = quiver_reasons

        if not market_ok:
            rejected.append(f"{symbol}:market_gate")
            rejection_counts["market_gate"] += 1
            decision_trace["final_decision"] = "REJECT"
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )
            continue
        if not quiver_gate_ok:
            rejected.append(f"{symbol}:quiver_gate")
            rejection_counts["quiver_gate"] += 1
            decision_trace["final_decision"] = "REJECT"
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )
            continue

        decision_trace.update(
            {
                "features_used": _compact_features(features),
                "score_total": round(total_score, 4),
            }
        )
        approval_threshold = _signal_threshold()
        if approval_threshold and total_score < approval_threshold:
            rejected.append(f"{symbol}:score_threshold")
            rejection_counts["score_threshold"] += 1
            decision_trace["final_decision"] = "REJECT"
            decision_trace["score_threshold"] = approval_threshold
            decision_trace["score_reasons"] = ["score_below_threshold"]
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )
            continue

        current_price = features.get("yahoo_current_price")
        atr = features.get("yahoo_atr")
        atr_pct = features.get("yahoo_atr_pct")
        volume_7d = features.get("yahoo_volume_7d_avg")

        candidates.append(
            {
                "symbol": symbol,
                "score_total": total_score,
                "quiver_score": quiver_score,
                "price": current_price,
                "atr": atr,
                "atr_pct": atr_pct,
                "volume_7d": volume_7d,
                "quiver_strength": sum(
                    max(0.0, _normalize_feature_value(k, float(v)))
                    for k, v in features.items()
                    if k.startswith("quiver_")
                ),
                "decision_trace": decision_trace,
                "features": features,
                "yahoo_symbol_used": yahoo_meta.get("used_symbol"),
                "quiver_symbol_used": quiver_symbol,
                "provider_fallback_used": provider_fallback_used,
            }
        )

    mapping_fail_pct = (yahoo_missing / max(len(evaluated), 1)) if evaluated else 0.0
    mapping_threshold = float(_universe_cfg().get("mapping_failure_pct_block", 0.6))
    if mapping_fail_pct > mapping_threshold:
        log_event(
            f"SCAN universe mapping issue suspected fail_pct={mapping_fail_pct:.2f}",
            event="SCAN",
        )
        return []

    ranked = sorted(
        candidates,
        key=lambda c: (
            c["score_total"],
            c["quiver_strength"],
            c.get("volume_7d") or 0.0,
        ),
        reverse=True,
    )

    approved_plans, rejection_reasons = risk_manager.plan_trades(ranked)
    approved: List[SignalTuple] = []
    for plan in approved_plans:
        decision_trace = plan.get("decision_trace", {})
        decision_trace["risk_check_passed"] = True
        decision_trace["final_decision"] = "APPROVE"
        log_event(
            f"TRACE {plan['symbol']} {json.dumps(decision_trace, separators=(',', ':'))}",
            event="TRACE",
        )
        approved.append(
            (
                plan["symbol"],
                float(plan["score_total"]),
                float(plan["quiver_score"]),
                float(plan["price"]) if plan["price"] is not None else None,
                float(plan["atr"]) if plan["atr"] is not None else None,
                plan,
            )
        )

    for rejection in rejection_reasons:
        decision_trace = rejection.get("decision_trace", {})
        if decision_trace:
            decision_trace["risk_check_passed"] = False
            risk_reasons = rejection.get("reasons", [])
            decision_trace["risk_reasons"] = risk_reasons
            decision_trace["final_decision"] = "REJECT"
            for reason in risk_reasons:
                rejection_counts[f"risk_{reason}"] += 1
            log_event(
                f"TRACE {rejection.get('symbol')} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )

    top_rejected_by_reason = rejection_counts.most_common(5)
    log_event(
        (
            "SCAN summary "
            f"evaluated={len(evaluated)} "
            f"yahoo_prefilter_pass={yahoo_prefilter_pass} "
            f"quiver_called={quiver_called} "
            f"candidates={len(candidates)} "
            f"approved={len(approved)} "
            f"market_gate={market_ok} "
            f"market_reasons={market_reasons} "
            f"mapping_fail_pct={mapping_fail_pct:.2f} "
            f"top_rejected_by_reason={top_rejected_by_reason}"
        ),
        event="SCAN",
    )
    if rejection_reasons:
        top_reasons = [rej.get("reasons", []) for rej in rejection_reasons[:5]]
        log_event(f"SCAN risk_rejections_top5={top_reasons}", event="SCAN")

    return approved
