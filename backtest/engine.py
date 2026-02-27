"""Backtest engine — indicators + position-protector simulation on daily OHLCV.

Standalone module: no imports from core/, signals/, broker/.
Reads config/policy.yaml for parameters but never modifies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TradeResult:
    symbol: str
    strategy: str        # "baseline" | "filtered"
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    exit_reason: str     # "stop" | "stop_gap" | "tp" | "time"
    pnl_pct: float       # % gain/loss
    r_multiple: float    # profit in units of initial risk
    # indicator values at entry (for diagnostics)
    adx14: float = 0.0
    rs_vs_spy: float = 0.0
    hi52w_pct: float = 0.0
    ema_aligned: bool = False


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

def _wilder(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder smoothing — same as EWM with alpha=1/period."""
    return series.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute_indicators(hist: pd.DataFrame, spy_hist: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Enrich OHLCV DataFrame with all indicators used by entry signals."""
    df = hist.copy()
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    prev_close = close.shift(1)

    # --- ATR-14 (same formula as position_protector._atr) ---
    tr = (
        (high - low)
        .combine((high - prev_close).abs(), max)
        .combine((low  - prev_close).abs(), max)
    )
    df["atr14"] = tr.rolling(14, min_periods=14).mean()

    # --- ADX-14 ---
    up   = high - high.shift(1)
    down = low.shift(1) - low
    plus_dm  = np.where((up > down) & (up > 0),   up,   0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_di  = 100 * _wilder(pd.Series(plus_dm,  index=df.index)) / _wilder(tr).replace(0, np.nan)
    minus_di = 100 * _wilder(pd.Series(minus_dm, index=df.index)) / _wilder(tr).replace(0, np.nan)
    di_diff  = (plus_di - minus_di).abs()
    di_sum   = (plus_di + minus_di).replace(0, np.nan)
    df["adx14"] = _wilder(100 * di_diff / di_sum)

    # --- EMA9 / EMA21 ---
    df["ema9"]       = close.ewm(span=9,  min_periods=9,  adjust=False).mean()
    df["ema21"]      = close.ewm(span=21, min_periods=21, adjust=False).mean()
    df["ema_aligned"] = (df["ema9"] > df["ema21"]).astype(bool)

    # --- 52-week high proximity ---
    df["hi52w"]     = close.rolling(252, min_periods=20).max()
    df["hi52w_pct"] = close / df["hi52w"].replace(0, np.nan)

    # --- SMA-20 (used in baseline entry) ---
    df["sma20"] = close.rolling(20, min_periods=20).mean()

    # --- RSI-14 (used in baseline entry) ---
    delta    = close.diff()
    avg_gain = _wilder(delta.clip(lower=0))
    avg_loss = _wilder((-delta).clip(lower=0))
    df["rsi14"] = 100 - 100 / (1 + avg_gain / avg_loss.replace(0, np.nan))

    # --- Relative Strength vs SPY (20-day return delta) ---
    if spy_hist is not None and not spy_hist.empty:
        spy_ret = spy_hist["Close"].pct_change(20).reindex(df.index, method="pad")
        df["rs_vs_spy"] = close.pct_change(20) - spy_ret
    else:
        df["rs_vs_spy"] = 0.0

    return df


def mark_entries(df: pd.DataFrame) -> pd.DataFrame:
    """Add boolean columns for baseline and filtered entry signals."""
    valid = df["atr14"].notna() & df["sma20"].notna() & df["rsi14"].notna()

    # Baseline: price > SMA20, RSI not overbought/oversold
    df["entry_baseline"] = (
        valid
        & (df["Close"] > df["sma20"])
        & (df["rsi14"] >= 30)
        & (df["rsi14"] <= 70)
    )

    # Filtered: baseline + 4 extra technical conditions
    df["entry_filtered"] = (
        df["entry_baseline"]
        & (df["adx14"] >= 20)           # confirmed trend strength
        & (df["hi52w_pct"] >= 0.65)     # within 35% of 52-week high
        & df["ema_aligned"]             # fast EMA above slow EMA
        & (df["rs_vs_spy"] > 0)         # outperforming the market
    )

    return df


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    strategy: str,
    policy: dict,
    max_hold_days: int = 30,
) -> Optional[TradeResult]:
    """Replay position-protector logic day-by-day from entry_idx.

    Mirrors tick_protect_positions() using the same policy.yaml parameters.
    """
    risk_cfg      = policy.get("risk", {})
    exec_cfg      = policy.get("execution", {})
    safeguards_cfg = policy.get("safeguards", {})

    atr_k               = float(risk_cfg.get("atr_k", 2.0))
    min_stop_pct        = float(risk_cfg.get("min_stop_pct", 0.05))
    tp_mult             = float(exec_cfg.get("take_profit_atr_mult", 3.0))
    trailing_mult       = float(exec_cfg.get("trailing_stop_atr_mult", 2.0))
    trailing_profit_mult = float(exec_cfg.get("trailing_stop_profit_atr_mult", 1.0))
    tighten_at_r        = float(exec_cfg.get("trailing_tighten_at_R", 0.5))
    break_even_r        = float(safeguards_cfg.get("break_even_R", 1.0))
    break_even_buf      = float(safeguards_cfg.get("break_even_buffer_pct", 0.001))

    row = df.iloc[entry_idx]
    entry_price = float(row["Close"])
    atr         = float(row["atr14"])

    if atr <= 0 or entry_price <= 0:
        return None

    stop_dist     = max(atr_k * atr, min_stop_pct * entry_price)
    initial_stop  = entry_price - stop_dist
    tp_price      = entry_price + tp_mult * atr
    current_stop  = initial_stop

    symbol     = df.attrs.get("symbol", "?")
    entry_date = str(df.index[entry_idx].date())

    adx_val    = float(row.get("adx14", 0) or 0)
    rs_val     = float(row.get("rs_vs_spy", 0) or 0)
    hi52w_val  = float(row.get("hi52w_pct", 0) or 0)
    ema_val    = bool(row.get("ema_aligned", False))

    for day in range(1, max_hold_days + 1):
        fi = entry_idx + day
        if fi >= len(df):
            break

        bar       = df.iloc[fi]
        bar_open  = float(bar["Open"])
        bar_high  = float(bar["High"])
        bar_low   = float(bar["Low"])
        bar_close = float(bar["Close"])
        bar_atr   = float(bar["atr14"]) if pd.notna(bar.get("atr14")) else atr
        bar_date  = str(df.index[fi].date())

        def _result(price: float, reason: str) -> TradeResult:
            risk   = max(entry_price - initial_stop, 1e-9)
            r_mult = (price - entry_price) / risk
            return TradeResult(
                symbol=symbol, strategy=strategy,
                entry_date=entry_date, exit_date=bar_date,
                entry_price=entry_price, exit_price=price,
                exit_reason=reason,
                pnl_pct=(price - entry_price) / entry_price,
                r_multiple=r_mult,
                adx14=adx_val, rs_vs_spy=rs_val,
                hi52w_pct=hi52w_val, ema_aligned=ema_val,
            )

        # Gap down through stop
        if bar_open <= current_stop:
            return _result(bar_open, "stop_gap")

        # Intraday stop
        if bar_low <= current_stop:
            return _result(current_stop, "stop")

        # Take profit
        if bar_high >= tp_price:
            return _result(tp_price, "tp")

        # End-of-day: update trailing stop (same logic as position_protector)
        denom      = max(entry_price - initial_stop, entry_price * min_stop_pct)
        r_multiple = (bar_close - entry_price) / denom if denom > 0 else 0.0

        if r_multiple >= break_even_r:
            be_stop = entry_price * (1 + break_even_buf)
            if be_stop > current_stop:
                current_stop = be_stop

        in_profit  = r_multiple >= tighten_at_r
        eff_mult   = trailing_profit_mult if in_profit else trailing_mult
        trail_stop = bar_close - bar_atr * eff_mult
        if trail_stop > current_stop:
            current_stop = trail_stop

    # Time limit
    last_idx   = min(entry_idx + max_hold_days, len(df) - 1)
    exit_price = float(df.iloc[last_idx]["Close"])
    exit_date  = str(df.index[last_idx].date())
    risk       = max(entry_price - initial_stop, 1e-9)
    return TradeResult(
        symbol=symbol, strategy=strategy,
        entry_date=entry_date, exit_date=exit_date,
        entry_price=entry_price, exit_price=exit_price,
        exit_reason="time",
        pnl_pct=(exit_price - entry_price) / entry_price,
        r_multiple=(exit_price - entry_price) / risk,
        adx14=adx_val, rs_vs_spy=rs_val,
        hi52w_pct=hi52w_val, ema_aligned=ema_val,
    )


# ---------------------------------------------------------------------------
# Full symbol backtest
# ---------------------------------------------------------------------------

def run_symbol(
    symbol: str,
    hist: pd.DataFrame,
    spy_hist: Optional[pd.DataFrame],
    policy: dict,
    cooldown_days: int = 20,
) -> list[TradeResult]:
    """Run all entry/exit simulations for one symbol."""
    hist.attrs["symbol"] = symbol
    df = compute_indicators(hist, spy_hist)
    df = mark_entries(df)

    results: list[TradeResult] = []
    last: dict[str, int] = {"baseline": -cooldown_days, "filtered": -cooldown_days}

    for i in range(50, len(df) - 5):
        row = df.iloc[i]
        for strategy, col in (("filtered", "entry_filtered"), ("baseline", "entry_baseline")):
            if not bool(row.get(col, False)):
                continue
            if i - last[strategy] < cooldown_days:
                continue
            result = simulate_trade(df, i, strategy, policy)
            if result:
                results.append(result)
                last[strategy] = i

    return results
