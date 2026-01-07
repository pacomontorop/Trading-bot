"""Signal reader and scorer for long-only equity trades."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Iterable, List, Tuple

import config
from core import risk_manager
from signals.features import get_symbol_features
from signals.scoring import fetch_yahoo_snapshot
from utils.logger import log_event
from utils.universe import load_universe

SignalTuple = Tuple[str, float, float, float, float | None, float | None, dict]


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

YAHOO_FEATURE_WEIGHTS = {
    "yahoo_trend_positive": 0.5,
}

FEATURE_WEIGHTS: dict[str, float] = {}
if config.ENABLE_QUIVER:
    FEATURE_WEIGHTS.update(QUIVER_FEATURE_WEIGHTS)
if config.ENABLE_YAHOO:
    FEATURE_WEIGHTS.update(YAHOO_FEATURE_WEIGHTS)

_FEATURE_CAPS = {
    "quiver_gov_contract_total_amount": 200_000_000,
    "quiver_wsb_recent_max_mentions": 500,
    "quiver_insider_buy_count": 5,
    "quiver_gov_contract_count": 5,
    "quiver_sec13f_change_latest_pct": 20,
    "quiver_patent_momentum_latest": 5,
}


def _policy_section(name: str) -> dict:
    return (getattr(config, "_policy", {}) or {}).get(name, {}) or {}


def _signal_cfg() -> dict:
    return _policy_section("signals")


def _market_cfg() -> dict:
    return _policy_section("market")


def _yahoo_gate_cfg() -> dict:
    return _policy_section("yahoo_gate")


def _quiver_gate_cfg() -> dict:
    return _policy_section("quiver_gate")


def _universe_cfg() -> dict:
    return _policy_section("universe")


def _signal_threshold() -> float:
    return float(_signal_cfg().get("approval_threshold", 7.0))


def _normalize_feature_value(key: str, value: float) -> float:
    if value is None:
        return 0.0
    numeric = float(value)
    cap = _FEATURE_CAPS.get(key)
    if cap is not None:
        numeric = min(numeric, float(cap))
    return numeric


def _score_from_features(features: dict[str, float]) -> tuple[float, float, float]:
    """Simple score computed from numeric features only."""
    score = 0.0
    quiver_score = 0.0
    for key, weight in FEATURE_WEIGHTS.items():
        value = _normalize_feature_value(key, float(features.get(key, 0.0)))
        contribution = weight * value
        score += contribution
        if key.startswith("quiver_"):
            quiver_score += contribution
    return score, quiver_score, 0.0


def _compact_features(features: dict[str, float]) -> dict[str, float]:
    compact = {k: v for k, v in features.items() if k.startswith(("quiver_", "yahoo_"))}
    trimmed = {k: v for k, v in compact.items() if v not in (0, 0.0, None)}
    return dict(list(trimmed.items())[:8])


def _prefilter_yahoo(symbol: str, yahoo_symbol: str) -> tuple[bool, list[str], tuple, dict]:
    reasons: list[str] = []
    if not config.ENABLE_YAHOO:
        return False, ["yahoo_disabled"], (None, None, None, None, None, None, None, None), {
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
    if snapshot.status != "ok":
        return False, ["yahoo_missing"], snapshot.data, {
            "status": snapshot.status,
            "used_symbol": snapshot.used_symbol,
            "fallback_used": snapshot.fallback_used,
        }

    freshness_days = int(_signal_cfg().get("freshness_days_yahoo_prices", 2))
    if hist is None or hist.empty:
        reasons.append("yahoo_history_missing")
    else:
        last_dt = hist.index[-1]
        if isinstance(last_dt, datetime):
            last_dt = last_dt.tz_localize(timezone.utc) if last_dt.tzinfo is None else last_dt
            age_days = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
            if age_days > freshness_days:
                reasons.append("yahoo_stale")

    (
        market_cap,
        volume,
        weekly_change,
        trend_positive,
        price_change_24h,
        volume_7d,
        current_price,
        atr,
    ) = snapshot.data

    gate_cfg = _yahoo_gate_cfg()
    min_market_cap = float(gate_cfg.get("min_market_cap", 0))
    min_avg_volume = float(gate_cfg.get("min_avg_volume_7d", 0))
    min_price = float(gate_cfg.get("min_price", 0))
    max_price = float(gate_cfg.get("max_price", float("inf")))
    max_atr_pct = float(gate_cfg.get("max_atr_pct", float("inf")))
    require_trend = bool(gate_cfg.get("require_trend_positive", False))

    if not current_price or current_price <= 0:
        reasons.append("invalid_price")
    if min_market_cap and (market_cap or 0) < min_market_cap:
        reasons.append("market_cap_low")
    if min_avg_volume and (volume_7d or 0) < min_avg_volume:
        reasons.append("volume_low")
    if min_price and (current_price or 0) < min_price:
        reasons.append("price_below_min")
    if max_price != float("inf") and (current_price or 0) > max_price:
        reasons.append("price_above_max")
    if current_price and atr:
        atr_pct = (float(atr) / float(current_price)) * 100.0
        if atr_pct > max_atr_pct:
            reasons.append("atr_pct_high")
    if require_trend and not trend_positive:
        reasons.append("trend_negative")

    ok = not reasons
    return ok, reasons, snapshot.data, {
        "status": snapshot.status,
        "used_symbol": snapshot.used_symbol,
        "fallback_used": snapshot.fallback_used,
    }


def gate_market_conditions() -> tuple[bool, list[str], dict]:
    reasons: list[str] = []
    cfg = _market_cfg()
    if cfg.get("global_kill_switch"):
        reasons.append("global_kill_switch")
    return not reasons, reasons, {}


def gate_quiver_minimum(features: dict[str, float]) -> tuple[bool, list[str]]:
    cfg = _quiver_gate_cfg()
    reasons: list[str] = []
    if not config.ENABLE_QUIVER:
        if not bool(_signal_cfg().get("allow_trade_without_quiver", False)):
            return False, ["quiver_disabled"]
        return True, []

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
    wsb_min = float(cfg.get("wsb_mentions_min", 0))
    if wsb_min > 0:
        checks.append(features.get("quiver_wsb_recent_max_mentions", 0) >= wsb_min)
    sec_change_min = float(cfg.get("sec13f_change_min_pct", 0))
    if sec_change_min > 0:
        checks.append(features.get("quiver_sec13f_change_latest_pct", 0) >= sec_change_min)

    if checks and not any(checks):
        reasons.append("quiver_min_signal")
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

    universe = _load_universe()
    if not universe:
        log_event("SCAN no symbols to evaluate", event="SCAN")
        return []

    sample_maps = [u["ticker_map"] for u in universe[:5]]
    log_event(f"SCAN ticker_map_sample={sample_maps}", event="SCAN")

    exclude_set = {s.upper() for s in (exclude or [])}
    evaluated: list[str] = []
    candidates: list[dict] = []
    rejected: list[str] = []
    quiver_called = 0
    yahoo_prefilter_pass = 0
    yahoo_missing = 0

    market_ok, market_reasons, market_snapshot = gate_market_conditions()

    for entry in universe:
        if len(evaluated) >= max_symbols:
            break
        symbol = entry["ticker_map"]["canonical"]
        if symbol in exclude_set:
            rejected.append(f"{symbol}:excluded")
            continue

        evaluated.append(symbol)

        yahoo_symbol = entry["ticker_map"]["yahoo"]
        quiver_symbol = entry["ticker_map"]["quiver"]
        provider_fallback_used = False

        yahoo_ok, yahoo_reasons, yahoo_snapshot, yahoo_meta = _prefilter_yahoo(symbol, yahoo_symbol)
        if yahoo_meta.get("status") == "missing":
            yahoo_missing += 1
        if yahoo_meta.get("fallback_used"):
            provider_fallback_used = True

        decision_trace = {
            "symbol": symbol,
            "yahoo_symbol_used": yahoo_meta.get("used_symbol"),
            "quiver_symbol_used": quiver_symbol,
            "provider_fallback_used": provider_fallback_used,
            "yahoo_prefilter_pass": yahoo_ok,
            "yahoo_prefilter_reasons": yahoo_reasons,
            "market_reasons": market_reasons,
            "quiver_fetch_status": "disabled" if not config.ENABLE_QUIVER else "pending",
            "gates_passed": {
                "market": market_ok,
                "yahoo": yahoo_ok,
                "quiver": False,
            },
            "risk_check_passed": False,
            "final_decision": "REJECT",
        }

        if not yahoo_ok:
            rejected.append(f"{symbol}:yahoo_prefilter")
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )
            continue

        yahoo_prefilter_pass += 1

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
            )
            decision_trace["quiver_fetch_status"] = quiver_status
        except Exception as exc:
            decision_trace["quiver_fetch_status"] = "fail"
            decision_trace["final_decision"] = "REJECT"
            rejected.append(f"{symbol}:feature_error")
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))} err={exc}",
                event="TRACE",
            )
            continue

        total_score, quiver_score, fmp_score = _score_from_features(features)
        quiver_gate_ok, quiver_reasons = gate_quiver_minimum(features)
        decision_trace["gates_passed"]["quiver"] = quiver_gate_ok

        if not market_ok:
            rejected.append(f"{symbol}:market_gate")
            decision_trace["final_decision"] = "REJECT"
            log_event(
                f"TRACE {symbol} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )
            continue
        if not quiver_gate_ok:
            rejected.append(f"{symbol}:quiver_gate")
            decision_trace["final_decision"] = "REJECT"
            decision_trace["quiver_gate_reasons"] = quiver_reasons
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

        current_price = features.get("yahoo_current_price")
        atr = features.get("yahoo_atr")
        atr_pct = features.get("yahoo_atr_pct")
        volume_7d = features.get("yahoo_volume_7d_avg")

        candidates.append(
            {
                "symbol": symbol,
                "score_total": total_score,
                "quiver_score": quiver_score,
                "fmp_score": fmp_score,
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
            -(c.get("atr_pct") or 0.0),
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
                float(plan["fmp_score"]),
                float(plan["price"]) if plan["price"] is not None else None,
                float(plan["atr"]) if plan["atr"] is not None else None,
                plan,
            )
        )

    for rejection in rejection_reasons:
        decision_trace = rejection.get("decision_trace", {})
        if decision_trace:
            decision_trace["risk_check_passed"] = False
            decision_trace["risk_reasons"] = rejection.get("reasons", [])
            decision_trace["final_decision"] = "REJECT"
            log_event(
                f"TRACE {rejection.get('symbol')} {json.dumps(decision_trace, separators=(',', ':'))}",
                event="TRACE",
            )

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
            f"rejected_top5={rejected[:5]}"
        ),
        event="SCAN",
    )
    if rejection_reasons:
        top_reasons = [rej.get("reasons", []) for rej in rejection_reasons[:5]]
        log_event(f"SCAN risk_rejections_top5={top_reasons}", event="SCAN")

    return approved
