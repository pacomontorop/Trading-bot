"""Manual scoring test — run any time (market closed, weekends, etc.).

Fetches real Yahoo + Quiver data for a fixed list of tickers and prints
the full score breakdown using the current weights in reader.py.
No orders are placed. Safe to run in Render or locally.

Usage:
    python test_scoring_manual.py
    python test_scoring_manual.py --threshold 10
"""

from __future__ import annotations

import os
import sys
import argparse

# Bootstrap credentials so imports work without a real .env
os.environ.setdefault("APCA_API_KEY_ID", "PKTEST00000000000000")
os.environ.setdefault("APCA_API_SECRET_KEY", "test_secret_key_placeholder_00000000000000")
os.environ.setdefault("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

import config  # noqa: E402 — must come after env setup

from signals.features import get_symbol_features
from signals.reader import (
    QUIVER_FEATURE_WEIGHTS,
    YAHOO_FEATURE_WEIGHTS,
    _FEATURE_CAPS,
    _normalize_feature_value,
)
from signals.scoring import fetch_yahoo_snapshot

# ── Test universe ────────────────────────────────────────────────────────────
# Mix of: tech, defense (gov contracts), financial, consumer, congressional buys
# Congressional signals: +3-5% annual alpha vs S&P (Grok 2026 analysis)
TEST_SYMBOLS = [
    # Defense / gov contracts (Quiver govcontracts signal)
    "LMT",    # Lockheed — heavy gov contracts
    "GEV",    # GE Vernova — defense/energy gov contracts
    "RTX",    # Raytheon — defense contracts + insider buys
    # Congressional buys (Quiver house/senate/congress signal)
    "UBER",   # Uber — frequent congressional purchase target
    "META",   # Meta — congressional buys + insider activity
    "FOUR",   # Shift4 Payments — congressional buy candidate
    "THRY",   # Thryv Holdings — small congressional buy
    # Insider buys (net positive)
    "WRB",    # W.R. Berkley — financial, insider buys
    "NRP",    # Natural Resource Partners — gov/commodities
    # Tech / patent momentum
    "ANET",   # Arista — patent momentum, tech (insiders sell → reference reject)
    # Consumer / app ratings
    "CAKE",   # Cheesecake Factory — app ratings
    # Financial
    "RDN",    # Radian Group
    "HOFT",   # Hooker Furnishings — small cap
    # High insider sell reference (should reject with strong Quiver score)
    "PLTR",   # Palantir — gov contracts but heavy insider selling
]

# ── Score helpers (mirror reader.py logic) ───────────────────────────────────

def _score(features: dict) -> tuple[float, float, float]:
    """Return (total, quiver_score, yahoo_bonus)."""
    quiver = 0.0
    for key, weight in QUIVER_FEATURE_WEIGHTS.items():
        val = _normalize_feature_value(key, float(features.get(key, 0.0)))
        quiver += weight * val
    yahoo = 0.0
    for key, weight in YAHOO_FEATURE_WEIGHTS.items():
        val = _normalize_feature_value(key, float(features.get(key, 0.0)))
        yahoo += weight * val
    return quiver + yahoo, quiver, yahoo


def _fmt(value: float) -> str:
    return f"{value:+.2f}" if value != 0 else "  0.00"


# ── Main ─────────────────────────────────────────────────────────────────────

def main(threshold: float = 0.0) -> None:
    print(f"\n{'='*65}")
    print(f"  SCORE TEST — {config.ENABLE_QUIVER=}  {config.ENABLE_YAHOO=}")
    print(f"  Threshold: {threshold:.1f} pts  |  {len(TEST_SYMBOLS)} symbols")
    print(f"{'='*65}\n")

    results = []

    for symbol in TEST_SYMBOLS:
        print(f"▶ {symbol} — fetching ...", end="", flush=True)
        try:
            snapshot = fetch_yahoo_snapshot(symbol, return_history=True)
            snap_obj, hist = snapshot
            yahoo_snapshot = snap_obj.data
            yahoo_status = snap_obj.status

            features = get_symbol_features(
                symbol,
                yahoo_snapshot=yahoo_snapshot,
                yahoo_symbol=symbol,
                yahoo_hist=hist,
            )
        except Exception as exc:
            print(f"  ERROR: {exc}")
            continue

        total, quiver, yahoo_bonus = _score(features)
        decision = "✅ APPROVE" if total >= threshold else "❌ REJECT"
        print(f"\r▶ {symbol:<6}  total={total:6.2f}  quiver={quiver:6.2f}  yahoo={yahoo_bonus:5.2f}  {decision}")

        # Detailed breakdown — only non-zero features
        active_q = {k: features.get(k, 0) for k in QUIVER_FEATURE_WEIGHTS if features.get(k, 0) != 0}
        active_y = {k: features.get(k, 0) for k in YAHOO_FEATURE_WEIGHTS if features.get(k, 0) != 0}

        if active_q:
            print("   Quiver:")
            for k, v in active_q.items():
                capped = _normalize_feature_value(k, float(v))
                pts = QUIVER_FEATURE_WEIGHTS[k] * capped
                print(f"     {k:<42} raw={v:.3g}  pts={_fmt(pts)}")
        if active_y:
            print("   Yahoo:")
            for k, v in active_y.items():
                capped = _normalize_feature_value(k, float(v))
                pts = YAHOO_FEATURE_WEIGHTS[k] * capped
                print(f"     {k:<42} raw={v:.3g}  pts={_fmt(pts)}")

        results.append((symbol, total, quiver, yahoo_bonus, decision))
        print()

    # Summary
    print(f"\n{'='*65}")
    print(f"  SUMMARY (sorted by total score)")
    print(f"{'='*65}")
    for sym, total, quiver, yahoo, dec in sorted(results, key=lambda x: -x[1]):
        print(f"  {sym:<6}  total={total:6.2f}  quiver={quiver:6.2f}  yahoo={yahoo:5.2f}  {dec}")
    approved = [r for r in results if r[1] >= threshold]
    print(f"\n  Approved: {len(approved)}/{len(results)}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manual scoring test")
    parser.add_argument("--threshold", type=float, default=0.0,
                        help="Approval threshold in pts (default: 0 = show all)")
    args = parser.parse_args()
    main(threshold=args.threshold)
