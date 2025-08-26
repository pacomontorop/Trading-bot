#test_backtest_from_trades.py

from backtest_from_trades import analyze_trades


def test_analyze_trades_basic():
    trades = [
        {"symbol": "A", "signal": "s1", "pnl_usd": "10"},
        {"symbol": "B", "signal": "s2", "pnl_usd": "-5"},
        {"symbol": "A", "signal": "s1", "pnl_usd": "20"},
        {"symbol": "C", "signal": "s1", "pnl_usd": "-10"},
        {"symbol": "B", "signal": "s3", "pnl_usd": "5"},
    ]
    stats = analyze_trades(trades)
    assert stats["total_trades"] == 5
    assert stats["total_pnl"] == 20
    assert round(stats["win_rate"], 2) == 60.0
    assert stats["average_pnl"] == 4.0
    assert round(stats["max_drawdown"], 2) == 10.0
    assert round(stats["sharpe_ratio"], 2) == 0.84
    assert stats["top_symbols"][0][0] == "A"
    assert stats["bottom_symbols"][0][0] == "C"
