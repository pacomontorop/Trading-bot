from __future__ import annotations

from typing import Tuple, Dict

from broker.alpaca import is_market_open
from signals.filters import is_position_open
from utils.logger import log_event
from utils import metrics
from utils.symbols import detect_asset_class


def passes_long_gate(symbol: str, data_ctx=None) -> Tuple[bool, Dict]:
    """Safety gate for long trades (market state + tradable equity)."""
    reasons: Dict[str, str] = {}

    if not is_market_open():
        reasons["market"] = "closed"

    asset_class = detect_asset_class(symbol)
    if asset_class != "equity":
        reasons["asset_class"] = asset_class

    if is_position_open(symbol):
        reasons["position"] = "already_open"

    ok = not reasons
    payload = reasons or {"status": "ok"}
    summary = ", ".join(f"{k}={v}" for k, v in payload.items())
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
    return ok, ({} if ok else reasons)
