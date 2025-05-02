import yfinance as yf

def fetch_yfinance_stock_data(symbol):
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        market_cap = info.get("marketCap")
        volume = info.get("volume")
        hist = ticker.history(period="7d", interval="1d")

        weekly_change = None
        if len(hist) >= 2:
            weekly_change = ((hist['Close'][-1] - hist['Close'][0]) / hist['Close'][0]) * 100

        trend_positive = hist['Close'][-1] > hist['Close'][0] if len(hist) >= 2 else None
        price_change_24h = abs((hist['Close'][-1] - hist['Close'][-2]) / hist['Close'][-2]) * 100 if len(hist) >= 2 else None
        volume_7d_avg = hist['Volume'].mean() if not hist['Volume'].isna().all() else None

        print(f"📊 {symbol} | MC: {market_cap}, V: {volume}, Δ7d: {weekly_change}, Trend: {trend_positive}, Δ24h: {price_change_24h}, V_avg: {volume_7d_avg}")

        return market_cap, volume, weekly_change, trend_positive, price_change_24h, volume_7d_avg
    except Exception as e:
        print(f"❌ Error en fetch_yfinance_stock_data para {symbol}: {e}")
        return None, None, None, None, None, None
