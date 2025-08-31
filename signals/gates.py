from __future__ import annotations
from typing import Tuple, Dict, List
from broker.alpaca import is_market_open, get_current_price
from signals.scoring import fetch_yfinance_stock_data
from utils.state import already_evaluated_today, already_executed_today
from signals.reader import is_blacklisted_recent_loser
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


STRONG_QUIVER_KEYS = [
    "insider_buy_more_than_sell",
    "has_gov_contract",
    "positive_patent_momentum",
]


def passes_long_gate(symbol: str) -> Tuple[bool, Dict]:
    """Hard gate for long trades. Returns (ok, details)."""
    reasons: Dict[str, str] = {}
    details: Dict[str, List[str] | bool] = {}

    if already_evaluated_today(symbol):
        reasons["duplicate"] = "already_evaluated"
    if already_executed_today(symbol):
        reasons["duplicate"] = "already_executed"
    if reasons:
        return False, reasons

    if not is_market_open():
        reasons["market"] = "closed"
    mc, vol, *_ = fetch_yfinance_stock_data(symbol)
    if mc is None or mc < LIQ_MIN_MKTCAP:
        reasons["liquidity"] = "market_cap"
    if vol is None or vol < LIQ_MIN_AVG_VOL20:
        reasons["liquidity"] = "volume"
    price = get_current_price(symbol)
    if price is None or price < MIN_PRICE:
        reasons["price"] = "min_price"

    q_signals = fetch_quiver_signals(symbol) or {}
    strong = []
    recency_ok = False
    for key in STRONG_QUIVER_KEYS:
        data = q_signals.get(key)
        if data:
            strong.append(key)
            if data.get("age", 999) <= STRONG_SIGNAL_MAX_AGE_DAYS:
                recency_ok = True
    if not strong or not recency_ok:
        reasons["quiver"] = "weak_or_stale"
    details["quiver_strong"] = strong

    if is_blacklisted_recent_loser(symbol) and len(strong) < 2:
        reasons["recent_loser"] = "cooldown"

    ok = not reasons
    return ok, (details if ok else reasons)
def _default_fetch(symbol: str):
    from signals import quiver_utils
    return quiver_utils.fetch_quiver_signals(symbol)

fetch_quiver_signals = _default_fetch
