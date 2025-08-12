import yfinance as yf


def adjust_by_volatility(symbol: str, amount: int, lookback: int = 20) -> int:
    """Reduce la inversión si la volatilidad histórica es alta."""
    try:
        hist = yf.download(symbol, period=f"{lookback}d", interval="1d", progress=False)
        if hist.empty or "Close" not in hist:
            return amount
        pct = hist["Close"].pct_change().dropna()
        vol = pct.std()
        if vol > 0.05:
            return int(amount * 0.5)
        if vol > 0.03:
            return int(amount * 0.75)
    except Exception:
        return amount
    return int(amount)
