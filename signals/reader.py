import pandas as pd
from signals.filters import is_position_open, is_approved_by_finnhub
from broker.alpaca import api
from signals.scoring import fetch_yfinance_stock_data

assert callable(fetch_yfinance_stock_data), "❌ fetch_yfinance_stock_data no está correctamente definida o importada"

local_sp500_symbols = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "BRK.B", "UNH", "JNJ",
    "XOM", "JPM", "V", "PG", "MA", "HD", "CVX", "MRK", "LLY", "PEP", "ABBV", "AVGO",
    "COST", "KO", "ADBE", "PFE", "CSCO", "WMT", "ACN", "MCD", "DHR", "BAC", "TMUS",
    "NFLX", "VZ", "INTC", "LIN", "CRM", "ABT", "TMO", "DIS", "BMY", "NEE", "TXN",
    "AMGN", "PM", "LOW", "UNP", "ORCL", "MS", "RTX"
]

CRITERIA_WEIGHTS = {
    "market_cap": 2,
    "volume": 2,
    "weekly_change_positive": 1,
    "trend_positive": 1,
    "volatility_ok": 1,
    "volume_growth": 1
}

def fetch_sp500_symbols():
    try:
        sp500_url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        tables = pd.read_html(sp500_url)
        return tables[0]['Symbol'].tolist()
    except:
        return local_sp500_symbols

stock_assets = fetch_sp500_symbols()

def get_top_signals(min_criteria=5, verbose=False):
    opportunities = []
    already_considered = set()

    for symbol in stock_assets:
        if symbol in already_considered or is_position_open(symbol):
            continue
        already_considered.add(symbol)

        try:
            market_cap, volume, weekly_change, trend, price_change_24h, volume_7d_avg = fetch_yfinance_stock_data(symbol)

            score = 0
            if market_cap and market_cap > 500_000_000:
                score += CRITERIA_WEIGHTS["market_cap"]
            if volume and volume > 50_000_000:
                score += CRITERIA_WEIGHTS["volume"]
            if weekly_change is not None and weekly_change > 5:
                score += CRITERIA_WEIGHTS["weekly_change_positive"]
            if trend:
                score += CRITERIA_WEIGHTS["trend_positive"]
            if price_change_24h is not None and price_change_24h < 15:
                score += CRITERIA_WEIGHTS["volatility_ok"]
            if volume and volume_7d_avg and volume > volume_7d_avg:
                score += CRITERIA_WEIGHTS["volume_growth"]

            if verbose:
                print(f"🔍 {symbol}: score={score}, weekly_change={weekly_change}, trend={trend}, price_change_24h={price_change_24h}")

            if score >= min_criteria and is_approved_by_finnhub(symbol):
                opportunities.append((symbol, score))

        except Exception as e:
            print(f"❌ Error checking {symbol}: {e}")

    if not opportunities:
        return []

    opportunities.sort(key=lambda x: x[1], reverse=True)
    top_score = opportunities[0][1]

    seen = set()
    top_symbols = []
    for s, sc in opportunities:
        if sc < top_score:
            break
        if s not in seen:
            top_symbols.append(s)
            seen.add(s)
        if len(top_symbols) >= 3:
            break

    return top_symbols

def get_top_shorts(min_criteria=5, verbose=False):
    shorts = []
    for symbol in stock_assets:
        try:
            if is_position_open(symbol):
                continue

            market_cap, volume, weekly_change, trend, price_change_24h, volume_7d_avg = fetch_yfinance_stock_data(symbol)

            score = 0
            if market_cap and market_cap > 500_000_000:
                score += CRITERIA_WEIGHTS["market_cap"]
            if volume and volume > 50_000_000:
                score += CRITERIA_WEIGHTS["volume"]
            if weekly_change is not None and weekly_change < -5:
                score += CRITERIA_WEIGHTS["weekly_change_positive"]
            if trend is False:
                score += CRITERIA_WEIGHTS["trend_positive"]
            if price_change_24h is not None and price_change_24h < 15:
                score += CRITERIA_WEIGHTS["volatility_ok"]
            if volume and volume_7d_avg and volume > volume_7d_avg:
                score += CRITERIA_WEIGHTS["volume_growth"]

            if score >= min_criteria and is_approved_by_finnhub(symbol):
                if verbose:
                    print(f"🔻 {symbol}: {score} puntos (SHORT)")
                shorts.append((symbol, score))
        except Exception as e:
            print(f"❌ Error en short scan {symbol}: {e}")

    if not shorts:
        return []

    shorts.sort(key=lambda x: x[1], reverse=True)
    top_score = shorts[0][1]

    seen = set()
    top_symbols = []
    for s, sc in shorts:
        if sc < top_score:
            break
        if s not in seen:
            top_symbols.append(s)
            seen.add(s)
        if len(top_symbols) >= 3:
            break

    return top_symbols

