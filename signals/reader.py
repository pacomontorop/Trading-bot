import pandas as pd
from signals.filters import is_position_open, is_approved_by_finnhub_and_alphavantage
from broker.alpaca import api
from signals.scoring import fetch_yfinance_stock_data

assert callable(fetch_yfinance_stock_data), "❌ fetch_yfinance_stock_data no está correctamente definida o importada"

local_sp500_symbols = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "BRK-B", "UNH", "JNJ",
    "XOM", "JPM", "V", "PG", "MA", "HD", "CVX", "MRK", "LLY", "PEP", "ABBV", "AVGO",
    "COST", "KO", "ADBE", "PFE", "CSCO", "WMT", "ACN", "MCD", "DHR", "BAC", "TMUS",
    "NFLX", "VZ", "INTC", "LIN", "CRM", "ABT", "TMO", "DIS", "BMY", "NEE", "TXN",
    "AMGN", "PM", "LOW", "UNP", "ORCL", "MS", "RTX"
]

CRITERIA_WEIGHTS = {
    "market_cap": 2,
    "volume": 2,
    "weekly_change_positive": 1,
    "trend_positive": 2,
    "volatility_ok": 1,
    "volume_growth": 1
}

STRICTER_WEEKLY_CHANGE_THRESHOLD = 7
STRICTER_VOLUME_THRESHOLD = 70_000_000

def fetch_sp500_symbols():
    try:
        sp500_url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        tables = pd.read_html(sp500_url)
        return tables[0]['Symbol'].tolist()
    except:
        return local_sp500_symbols

def is_options_enabled(symbol):
    try:
        asset = api.get_asset(symbol)
        return getattr(asset, 'options_enabled', False)
    except:
        return False

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
            if volume and volume > STRICTER_VOLUME_THRESHOLD:
                score += CRITERIA_WEIGHTS["volume"]
            if weekly_change is not None and weekly_change > STRICTER_WEEKLY_CHANGE_THRESHOLD:
                score += CRITERIA_WEIGHTS["weekly_change_positive"]
            if trend:
                score += CRITERIA_WEIGHTS["trend_positive"]
            if price_change_24h is not None and 0 < price_change_24h < 10:
                score += CRITERIA_WEIGHTS["volatility_ok"]
            if volume and volume_7d_avg and volume > volume_7d_avg:
                score += CRITERIA_WEIGHTS["volume_growth"]

            if verbose:
                print(f"🔍 {symbol}: score={score}, reasons: market_cap={market_cap}, volume={volume}, weekly_change={weekly_change}, trend={trend}, price_change_24h={price_change_24h}, volume_7d_avg={volume_7d_avg}, options_enabled={is_options_enabled(symbol)}")

            if score >= min_criteria and is_approved_by_finnhub_and_alphavantage(symbol):
                opportunities.append((symbol, score))
            else:
                if verbose:
                    print(f"⛔ {symbol} descartado: score={score} (min requerido: {min_criteria}) o no aprobado por Finnhub/AlphaVantage")

        except Exception as e:
            print(f"❌ Error checking {symbol}: {e}")

    if not opportunities:
        return []

    opportunities.sort(key=lambda x: x[1], reverse=True)
    top_symbols = [symbol for symbol, score in opportunities]
    return top_symbols


def get_top_shorts(min_criteria=5, verbose=False):
    shorts = []
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
            if volume and volume > STRICTER_VOLUME_THRESHOLD:
                score += CRITERIA_WEIGHTS["volume"]
            if weekly_change is not None and weekly_change < -STRICTER_WEEKLY_CHANGE_THRESHOLD:
                score += CRITERIA_WEIGHTS["weekly_change_positive"]
            if trend is False:
                score += CRITERIA_WEIGHTS["trend_positive"]
            if price_change_24h is not None and 0 < price_change_24h < 10:
                score += CRITERIA_WEIGHTS["volatility_ok"]
            if volume and volume_7d_avg and volume > volume_7d_avg:
                score += CRITERIA_WEIGHTS["volume_growth"]

            if verbose:
                print(f"🔻 {symbol}: {score} puntos (SHORT) weekly_change={weekly_change}, trend={trend}, price_change_24h={price_change_24h}, options_enabled={is_options_enabled(symbol)}")

            if score >= min_criteria and is_approved_by_finnhub_and_alphavantage(symbol):
                shorts.append((symbol, score))
            else:
                if verbose:
                    print(f"⛔ {symbol} descartado (short): score={score} (min requerido: {min_criteria}) o no aprobado por Finnhub/AlphaVantage")

        except Exception as e:
            print(f"❌ Error en short scan {symbol}: {e}")

    if not shorts:
        return []

    shorts.sort(key=lambda x: x[1], reverse=True)
    top_symbols = [symbol for symbol, score in shorts]
    return top_symbols


