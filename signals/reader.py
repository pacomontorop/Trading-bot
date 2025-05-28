#reader.py


import pandas as pd
from signals.filters import is_position_open, is_approved_by_finnhub_and_alphavantage
from signals.quiver_utils import get_all_quiver_signals, score_quiver_signals, QUIVER_APPROVAL_THRESHOLD
from broker.alpaca import api
from signals.scoring import fetch_yfinance_stock_data
import random


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

import csv

def fetch_symbols_from_csv(path="data/symbols.csv"):
    try:
        with open(path, newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            symbols = [row["Symbol"] for row in reader if row.get("Symbol")]
            random.shuffle(symbols)  # << Esta línea los baraja aleatoriamente
            print(f"📄 Se cargaron {len(symbols)} símbolos desde {path}")
            return symbols
    except Exception as e:
        print(f"❌ Error leyendo CSV de símbolos desde '{path}': {e}")
        return local_sp500_symbols

evaluated_symbols_today = set()


def is_options_enabled(symbol):
    try:
        asset = api.get_asset(symbol)
        return getattr(asset, 'options_enabled', False)
    except:
        return False

stock_assets = fetch_symbols_from_csv()

def get_top_signals(min_criteria=5, verbose=False):
    print("🧩 Entrando en get_top_signals()...")  # 🔍 Diagnóstico
    opportunities = []
    global evaluated_symbols_today

    for symbol in stock_assets:
        if symbol in evaluated_symbols_today or is_position_open(symbol):
            evaluated_symbols_today.add(symbol)
            continue
        already_considered.add(symbol)

        # Evaluar Quiver primero
        try:
            signals = get_all_quiver_signals(symbol)
            quiver_score = score_quiver_signals(signals)
            if quiver_score >= QUIVER_APPROVAL_THRESHOLD:
                if verbose:
                    print(f"✅ {symbol} aprobado por Quiver (score={quiver_score}) → señales activas: {[k for k, v in signals.items() if v]}")
                opportunities.append((symbol, 90 + quiver_score, "Quiver"))
                continue
        except Exception as e:
            print(f"⚠️ Error evaluando señales Quiver para {symbol}: {e}")

        # Evaluación técnica si no lo aprueba Quiver
        try:
            data = fetch_yfinance_stock_data(symbol)
            if not data or len(data) != 6 or any(d is None for d in data):
                if verbose:
                    print(f"⚠️ Datos incompletos para {symbol}. Se omite.")
                continue

            market_cap, volume, weekly_change, trend, price_change_24h, volume_7d_avg = data

            score = 0
            if market_cap > 500_000_000:
                score += CRITERIA_WEIGHTS["market_cap"]
            if volume > STRICTER_VOLUME_THRESHOLD:
                score += CRITERIA_WEIGHTS["volume"]
            if weekly_change > STRICTER_WEEKLY_CHANGE_THRESHOLD:
                score += CRITERIA_WEIGHTS["weekly_change_positive"]
            if trend:
                score += CRITERIA_WEIGHTS["trend_positive"]
            if 0 < price_change_24h < 10:
                score += CRITERIA_WEIGHTS["volatility_ok"]
            if volume > volume_7d_avg:
                score += CRITERIA_WEIGHTS["volume_growth"]

            if verbose:
                print(f"🔍 {symbol}: score={score}, reasons: market_cap={market_cap}, volume={volume}, weekly_change={weekly_change}, trend={trend}, price_change_24h={price_change_24h}, volume_7d_avg={volume_7d_avg}")

            if score >= min_criteria and is_approved_by_finnhub_and_alphavantage(symbol):
                opportunities.append((symbol, score, "Técnico"))
            elif verbose:
                print(f"⛔ {symbol} descartado: score={score} (min requerido: {min_criteria}) o no aprobado por Finnhub/AlphaVantage")

        except Exception as e:
            print(f"❌ Error checking {symbol}: {e}")

    if not opportunities:
        return []

    opportunities.sort(key=lambda x: x[1], reverse=True)
    return opportunities[:5]

def get_top_shorts(min_criteria=5, verbose=False):
    shorts = []
    already_considered = set()

    for symbol in stock_assets:
        if symbol in already_considered or is_position_open(symbol):
            continue
        already_considered.add(symbol)

        try:
            data = fetch_yfinance_stock_data(symbol)
            if not data or len(data) != 6 or any(d is None for d in data):
                if verbose:
                    print(f"⚠️ Datos incompletos para {symbol}. Se omite.")
                continue

            market_cap, volume, weekly_change, trend, price_change_24h, volume_7d_avg = data

            score = 0
            if market_cap > 500_000_000:
                score += CRITERIA_WEIGHTS["market_cap"]
            if volume > STRICTER_VOLUME_THRESHOLD:
                score += CRITERIA_WEIGHTS["volume"]
            if weekly_change < -STRICTER_WEEKLY_CHANGE_THRESHOLD:
                score += CRITERIA_WEIGHTS["weekly_change_positive"]
            if trend is False:
                score += CRITERIA_WEIGHTS["trend_positive"]
            if 0 < price_change_24h < 10:
                score += CRITERIA_WEIGHTS["volatility_ok"]
            if volume > volume_7d_avg:
                score += CRITERIA_WEIGHTS["volume_growth"]

            if verbose:
                print(f"🔻 {symbol}: score={score} (SHORT) → weekly_change={weekly_change}, trend={trend}, price_24h={price_change_24h}")

            if score >= min_criteria and is_approved_by_finnhub_and_alphavantage(symbol):
                shorts.append((symbol, score, "Técnico"))
            elif verbose:
                print(f"⛔ {symbol} descartado (short): score={score} o no aprobado por Finnhub/AlphaVantage")

        except Exception as e:
            print(f"❌ Error en short scan {symbol}: {e}")

    if not shorts:
        return []

    shorts.sort(key=lambda x: x[1], reverse=True)
    return shorts[:5]
