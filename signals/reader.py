"""Signal reader and scorer for long-only equity trades."""

from __future__ import annotations

import csv
import os
from typing import Iterable, List, Tuple

import config
from signals.filters import is_position_open
from signals.fmp_signals import get_fmp_signal_score
from signals.fmp_utils import get_fmp_grade_score
from signals.quiver_utils import fetch_quiver_signals, score_quiver_signals
from signals.scoring import fetch_yfinance_stock_data, YFPricesMissingError, SkipSymbol
from utils.logger import log_event
from utils.symbols import detect_asset_class

SignalTuple = Tuple[str, float, float, float, float, float | None]


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


def get_top_signals(
    *,
    max_symbols: int = 30,
    exclude: Iterable[str] | None = None,
) -> List[SignalTuple]:
    """Return a list of approved signals for the current scan cycle."""

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
        if is_position_open(symbol):
            rejected.append(f"{symbol}:position_open")
            continue

        evaluated.append(symbol)

        try:
            quiver_signals = fetch_quiver_signals(symbol)
        except Exception as exc:
            rejected.append(f"{symbol}:quiver_error")
            log_event(f"APPROVAL {symbol}: rejected reason=quiver_error {exc}", event="APPROVAL")
            continue

        quiver_score = score_quiver_signals(quiver_signals or {})
        grade_score = get_fmp_grade_score(symbol) or 0.0
        fmp_signal = get_fmp_signal_score(symbol)
        fmp_signal_score = float(fmp_signal.get("score", 0.0)) if isinstance(fmp_signal, dict) else 0.0
        fmp_score = grade_score + fmp_signal_score
        total_score = quiver_score + fmp_score

        try:
            data = fetch_yfinance_stock_data(symbol)
        except SkipSymbol as exc:
            rejected.append(f"{symbol}:yf_skip")
            log_event(f"APPROVAL {symbol}: rejected reason=yf_skip {exc}", event="APPROVAL")
            continue
        except YFPricesMissingError as exc:
            rejected.append(f"{symbol}:yf_missing")
            log_event(f"APPROVAL {symbol}: rejected reason=yf_missing {exc}", event="APPROVAL")
            continue

        current_price = data[6] if data and len(data) >= 8 else None
        atr = data[7] if data and len(data) >= 8 else None
        if current_price is None or current_price <= 0:
            rejected.append(f"{symbol}:invalid_price")
            log_event(
                f"APPROVAL {symbol}: rejected reason=invalid_price price={current_price}",
                event="APPROVAL",
            )
            continue

        if total_score < threshold:
            rejected.append(f"{symbol}:below_threshold")
            log_event(
                (
                    f"APPROVAL {symbol}: rejected reason=below_threshold "
                    f"score={total_score:.2f} threshold={threshold:.2f}"
                ),
                event="APPROVAL",
            )
            continue

        approved.append(
            (
                symbol,
                float(total_score),
                float(quiver_score),
                float(fmp_score),
                float(current_price),
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
