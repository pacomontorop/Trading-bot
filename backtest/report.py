"""Report formatting for backtest results.

Prints a side-by-side comparison: baseline vs filtered strategy.
"""

from __future__ import annotations

from typing import Optional
from backtest.engine import TradeResult


def _stats(results: list[TradeResult]) -> Optional[dict]:
    if not results:
        return None

    n           = len(results)
    wins        = [r for r in results if r.pnl_pct > 0]
    losses      = [r for r in results if r.pnl_pct <= 0]
    win_rate    = len(wins) / n * 100
    avg_pnl     = sum(r.pnl_pct for r in results) / n * 100
    avg_r       = sum(r.r_multiple for r in results) / n
    avg_win     = sum(r.pnl_pct for r in wins) / len(wins) * 100  if wins   else 0.0
    avg_loss    = sum(r.pnl_pct for r in losses) / len(losses) * 100 if losses else 0.0
    tp_count    = sum(1 for r in results if r.exit_reason == "tp")
    stop_count  = sum(1 for r in results if r.exit_reason in ("stop", "stop_gap"))
    time_count  = sum(1 for r in results if r.exit_reason == "time")

    # Expectancy: avg_win * win_rate - avg_loss * loss_rate (in %)
    loss_rate   = (1 - len(wins) / n) * 100
    expectancy  = (avg_win * win_rate - abs(avg_loss) * loss_rate) / 100

    return {
        "n":          n,
        "win_rate":   win_rate,
        "avg_pnl":    avg_pnl,
        "avg_r":      avg_r,
        "avg_win":    avg_win,
        "avg_loss":   avg_loss,
        "expectancy": expectancy,
        "tp_pct":     tp_count  / n * 100,
        "stop_pct":   stop_count / n * 100,
        "time_pct":   time_count / n * 100,
    }


def _bar(value: float, max_val: float = 100.0, width: int = 20) -> str:
    filled = int(round(value / max_val * width)) if max_val > 0 else 0
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def print_report(results: list[TradeResult]) -> None:
    baseline  = [r for r in results if r.strategy == "baseline"]
    filtered  = [r for r in results if r.strategy == "filtered"]
    b         = _stats(baseline)
    f         = _stats(filtered)

    W = 70
    print("\n" + "=" * W)
    print(" BACKTEST RESULTS — Baseline vs Filtered")
    print("=" * W)

    if not b and not f:
        print("No results to display.")
        return

    # Header
    print(f"\n{'Metric':<28} {'Baseline':>18} {'Filtered':>18}")
    print("-" * W)

    def row(label: str, bval, fval, fmt: str = ".1f", suffix: str = "") -> None:
        bs = f"{bval:{fmt}}{suffix}" if bval is not None else "—"
        fs = f"{fval:{fmt}}{suffix}" if fval is not None else "—"
        print(f"  {label:<26} {bs:>18} {fs:>18}")

    bv = lambda k: b[k] if b else None
    fv = lambda k: f[k] if f else None

    row("Trades",          bv("n"),         fv("n"),         fmt="d")
    row("Win rate",        bv("win_rate"),   fv("win_rate"),  suffix="%")
    row("Avg P&L / trade", bv("avg_pnl"),    fv("avg_pnl"),   suffix="%")
    row("Avg R-multiple",  bv("avg_r"),      fv("avg_r"),     fmt=".2f")
    row("Avg win",         bv("avg_win"),    fv("avg_win"),   suffix="%")
    row("Avg loss",        bv("avg_loss"),   fv("avg_loss"),  suffix="%")
    row("Expectancy",      bv("expectancy"), fv("expectancy"), fmt=".2f", suffix="%")
    print("-" * W)
    row("Exit: TP hit",    bv("tp_pct"),     fv("tp_pct"),    suffix="%")
    row("Exit: Stop hit",  bv("stop_pct"),   fv("stop_pct"),  suffix="%")
    row("Exit: Time limit",bv("time_pct"),   fv("time_pct"),  suffix="%")

    # Win rate visual bar
    print()
    if b:
        print(f"  Baseline  win rate  {_bar(b['win_rate'])}  {b['win_rate']:.1f}%")
    if f:
        print(f"  Filtered  win rate  {_bar(f['win_rate'])}  {f['win_rate']:.1f}%")

    # Delta summary
    if b and f and b["n"] > 0 and f["n"] > 0:
        print()
        print("  Delta (Filtered − Baseline):")
        delta_wr    = f["win_rate"]   - b["win_rate"]
        delta_pnl   = f["avg_pnl"]   - b["avg_pnl"]
        delta_r     = f["avg_r"]      - b["avg_r"]
        delta_exp   = f["expectancy"] - b["expectancy"]
        sign = lambda v: ("+" if v >= 0 else "")
        print(f"    Win rate   {sign(delta_wr)}{delta_wr:.1f}%   "
              f"Avg P&L {sign(delta_pnl)}{delta_pnl:.1f}%   "
              f"Avg R {sign(delta_r)}{delta_r:.2f}   "
              f"Expectancy {sign(delta_exp)}{delta_exp:.2f}%")

        verdict = []
        if delta_wr > 3:
            verdict.append("win rate improved")
        if delta_pnl > 0.5:
            verdict.append("avg P&L improved")
        if delta_r > 0.1:
            verdict.append("R-multiple improved")
        if f["n"] < b["n"] * 0.3:
            verdict.append("too few filtered trades (consider relaxing filters)")

        if verdict:
            print(f"\n  Verdict: {', '.join(verdict)}")
            if any("improved" in v for v in verdict) and "too few" not in " ".join(verdict):
                print("  → Filtered conditions add value. Consider adding to policy.yaml.")
            elif "too few" in " ".join(verdict):
                print("  → Filters are too strict. Relax one or more thresholds.")
        else:
            print("\n  Verdict: no meaningful improvement from the 4 extra filters.")

    # Per-symbol breakdown
    print("\n" + "=" * W)
    print(" PER-SYMBOL BREAKDOWN")
    print("=" * W)
    print(f"\n  {'Symbol':<8} {'Strategy':<10} {'N':>4} {'WR%':>6} {'AvgR':>6} {'AvgP&L':>8}  Exits (tp/stp/time)")
    print("  " + "-" * 65)

    symbols = sorted(set(r.symbol for r in results))
    for sym in symbols:
        for strat in ("baseline", "filtered"):
            sub = [r for r in results if r.symbol == sym and r.strategy == strat]
            if not sub:
                continue
            s = _stats(sub)
            tp   = sum(1 for r in sub if r.exit_reason == "tp")
            stop = sum(1 for r in sub if r.exit_reason in ("stop", "stop_gap"))
            time = sum(1 for r in sub if r.exit_reason == "time")
            print(f"  {sym:<8} {strat:<10} {s['n']:>4} {s['win_rate']:>5.1f}% "
                  f"{s['avg_r']:>5.2f}  {s['avg_pnl']:>+6.1f}%  "
                  f"{tp}/{stop}/{time}")

    print()
