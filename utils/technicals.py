import yfinance as yf
import pandas as pd
from typing import Optional


def get_rsi(symbol: str, period: int = 14) -> Optional[float]:
    """Return the latest RSI value for the symbol."""
    try:
        data = yf.download(symbol, period=f"{period * 3}d", interval="1d", progress=False)
        if data.empty or "Close" not in data:
            return None
        close = data["Close"].astype(float)
        delta = close.diff().dropna()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        latest = rsi.iloc[-1]
        if isinstance(latest, pd.Series):
            latest = latest.iloc[0]
        return float(latest)
    except Exception:
        return None


def get_moving_average(symbol: str, window: int = 7) -> Optional[float]:
    """Return simple moving average of closing price."""
    try:
        data = yf.download(symbol, period=f"{window * 3}d", interval="1d", progress=False)
        if data.empty or "Close" not in data:
            return None
        ma = data["Close"].tail(window).mean()
        if isinstance(ma, pd.Series):
            ma = ma.iloc[0]
        return float(ma)
    except Exception:
        return None


def is_extremely_volatile(symbol: str, lookback: int = 5, threshold: float = 0.08) -> bool:
    """Check if the symbol shows high volatility based on std dev of pct change."""
    try:
        data = yf.download(symbol, period=f"{lookback + 1}d", interval="1d", progress=False)
        if data.empty or "Close" not in data:
            return False
        pct_std = data["Close"].pct_change().dropna().std()
        if pd.isna(pct_std):
            return False
        return pct_std > threshold
    except Exception:
        return False
