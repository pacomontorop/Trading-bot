import pandas as pd
import numpy as np
from typing import List, Tuple


def run_backtest(prices: pd.Series, signals: List[Tuple[pd.Timestamp, str]], initial_capital: float = 10000.0) -> dict:
    """Simple backtesting engine using close prices and buy/sell signals.

    :param prices: Serie de precios de cierre indexada por fecha.
    :param signals: Lista de tuplas (fecha, 'buy'|'sell').
    :return: métricas básicas del rendimiento.
    """
    cash = initial_capital
    position = 0
    equity_curve = []
    prices = prices.sort_index()
    sig_iter = iter(sorted(signals, key=lambda x: x[0]))
    current_signal = next(sig_iter, None)
    for date, price in prices.items():
        while current_signal and current_signal[0] <= date:
            action = current_signal[1]
            if action == 'buy' and cash >= price:
                qty = cash / price
                cash -= qty * price
                position += qty
            elif action == 'sell' and position > 0:
                cash += position * price
                position = 0
            current_signal = next(sig_iter, None)
        equity_curve.append(cash + position * price)
    equity = pd.Series(equity_curve, index=prices.index)
    returns = equity.pct_change().dropna()
    sharpe = np.sqrt(252) * returns.mean() / returns.std() if not returns.empty else 0
    drawdown = (equity / equity.cummax() - 1).min()
    return {
        "final_equity": float(equity.iloc[-1]) if not equity.empty else initial_capital,
        "sharpe_ratio": float(sharpe),
        "max_drawdown": float(drawdown),
    }
