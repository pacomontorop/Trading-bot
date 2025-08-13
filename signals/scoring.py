
#scoring.py

import yfinance as yf
from datetime import datetime, timedelta


# Simple in-memory cache with TTL to avoid repeatedly hitting yfinance
_CACHE_TTL = timedelta(minutes=5)
_stock_cache = {}


def fetch_yfinance_stock_data(symbol, verbose: bool = False):
    now = datetime.utcnow()
    cached = _stock_cache.get(symbol)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        return cached["data"]
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        market_cap = info.get("marketCap")
        volume = info.get("volume")
        hist = ticker.history(period="7d", interval="1d")

        weekly_change = None
        if len(hist) >= 2:
            weekly_change = ((hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0]) * 100

        trend_positive = hist['Close'].iloc[-1] > hist['Close'].iloc[0] if len(hist) >= 2 else None
        price_change_24h = abs((hist['Close'].iloc[-1] - hist['Close'].iloc[-2]) / hist['Close'].iloc[-2]) * 100 if len(hist) >= 2 else None
        volume_7d_avg = hist['Volume'].mean() if not hist['Volume'].isna().all() else None

        if market_cap is None or volume is None:
            try:
                from signals.fmp_utils import stock_screener
                fmp_data = stock_screener(symbol=symbol, limit=1)
                if fmp_data:
                    item = fmp_data[0]
                    market_cap = market_cap or item.get("marketCap")
                    volume = volume or item.get("volume")
            except Exception as e:
                print(f"⚠️ FMP fallback error for {symbol}: {e}")

        if verbose:
            print(f"📊 {symbol} | MC: {market_cap}, V: {volume}, Δ7d: {weekly_change}, Trend: {trend_positive}, Δ24h: {price_change_24h}, V_avg: {volume_7d_avg}")

        data = (market_cap, volume, weekly_change, trend_positive, price_change_24h, volume_7d_avg)
        _stock_cache[symbol] = {"data": data, "ts": now}
        return data
    except Exception as e:
        print(f"❌ Error en fetch_yfinance_stock_data para {symbol}: {e}")
        try:
            from signals.fmp_utils import stock_screener
            fmp_data = stock_screener(symbol=symbol, limit=1)
            if fmp_data:
                item = fmp_data[0]
                data = (item.get('marketCap'), item.get('volume'), None, None, None, None)
                _stock_cache[symbol] = {"data": data, "ts": now}
                return data
        except Exception as e2:
            print(f"⚠️ FMP fallback error para {symbol}: {e2}")
        return None, None, None, None, None, None
