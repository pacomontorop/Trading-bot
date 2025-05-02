def fetch_yfinance_stock_data(symbol):
    try:
        stock = yf.Ticker(symbol)
        info = stock.info
        market_cap = info.get('marketCap', 0)
        volume = info.get('volume', 0)
        weekly_change = (info.get('regularMarketPrice', 0) - info.get('fiftyTwoWeekLow', 0)) / info.get('fiftyTwoWeekLow', 1) * 100
        trend = info.get('regularMarketChangePercent', 0) > 0
        price_change_24h = abs(info.get('regularMarketChangePercent', 0))
        volume_7d_avg = info.get('averageVolume', 0)
        return market_cap, volume, weekly_change, trend, price_change_24h, volume_7d_avg
    except:
        return (None,) * 6
