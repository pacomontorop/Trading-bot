from __future__ import annotations
from typing import Tuple, Dict
from broker.alpaca import is_market_open, get_current_price
from signals.scoring import fetch_yfinance_stock_data, SkipSymbol
from utils.state import already_evaluated_today, already_executed_today
from signals.reader import is_blacklisted_recent_loser
from utils.logger import log_event
from utils import metrics
from utils.symbols import detect_asset_class
import yaml
import os

_POLICY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "policy.yaml")
with open(_POLICY_PATH, "r", encoding="utf-8") as _f:
    _policy = yaml.safe_load(_f)
GATE_CFG = _policy.get("gate", {})
MIN_PRICE = float(GATE_CFG.get("min_price", 3.0))
LIQ_MIN_MKTCAP = float(GATE_CFG.get("min_cap", 500e6))
LIQ_MIN_AVG_VOL20 = float(GATE_CFG.get("min_avg_vol_20d", 500000))
STRONG_SIGNAL_MAX_AGE_DAYS = int(GATE_CFG.get("strong_signal_max_age_days", 3))


def _has_strong_recent_quiver_signal(symbol: str, max_age_days: float) -> bool:
    from signals.quiver_utils import (
        get_insider_signal,
        get_gov_contract_signal,
        get_patent_momentum_signal,
    )

    for fn in (get_insider_signal, get_gov_contract_signal, get_patent_momentum_signal):
        r = fn(symbol)
        if getattr(r, "active", False) and (r.days is not None) and (r.days <= max_age_days):
            log_event(
                f"GATE {symbol}: strong recent via {fn.__name__} age={r.days:.2f}d (≤ {max_age_days}d)"
            )
            return True
    return False


def passes_long_gate(symbol: str, data_ctx=None) -> Tuple[bool, Dict]:
    """Hard gate for long trades. Returns (ok, details)."""
    reasons: Dict[str, str] = {}
    details: Dict[str, bool] = {}

    if already_evaluated_today(symbol):
        reasons["duplicate"] = "already_evaluated"
    if already_executed_today(symbol):
        reasons["duplicate"] = "already_executed"
    if reasons:
        return False, reasons

    if not is_market_open():
        reasons["market"] = "closed"

    mc = vol = None
    asset_class = detect_asset_class(symbol)
    if asset_class != "equity":
        reasons["asset_class"] = asset_class
    else:
        try:
            mc, vol, *_ = fetch_yfinance_stock_data(symbol)
        except SkipSymbol as exc:
            reasons["asset_class"] = str(exc)
        except Exception:
            mc = vol = None

    if mc is None or mc < LIQ_MIN_MKTCAP:
        reasons["liquidity"] = "market_cap"
    if vol is None or vol < LIQ_MIN_AVG_VOL20:
        reasons["liquidity"] = "volume"
    price = get_current_price(symbol)
    if price is None or price < MIN_PRICE:
        reasons["price"] = "min_price"

    if not _has_strong_recent_quiver_signal(symbol, STRONG_SIGNAL_MAX_AGE_DAYS):
        reasons["quiver"] = "weak_or_stale"
        details["quiver_strong"] = False
    else:
        details["quiver_strong"] = True

    if is_blacklisted_recent_loser(symbol):
        reasons["recent_loser"] = "cooldown"

    ok = not reasons
    payload = details if ok else reasons
    summary = ", ".join(f"{k}={v}" for k, v in payload.items()) if payload else "ok"
    if ok:
        metrics.inc("gated")
        log_event(
            f"passed gate {summary}",
            event="GATE",
            symbol=symbol,
        )
    else:
        metrics.inc("rejected")
        log_event(
            f"failed gate {summary}",
            event="GATE",
            symbol=symbol,
        )
    return ok, (details if ok else reasons)
def _default_fetch(symbol: str):  # pragma: no cover - kept for backward compatibility
    from signals import quiver_utils
    return quiver_utils.fetch_quiver_signals(symbol)

fetch_quiver_signals = _default_fetch
