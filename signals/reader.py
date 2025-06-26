#reader.py


from signals.filters import (
    is_position_open,
    is_approved_by_finnhub_and_alphavantage,
    get_cached_positions,
)
from signals.quiver_utils import _async_is_approved_by_quiver
from signals.quiver_event_loop import run_in_quiver_loop
import asyncio
from broker.alpaca import api
from signals.scoring import fetch_yfinance_stock_data
from datetime import datetime





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

priority_symbols = [
    "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "BRK.B", "TSLA", "AVGO", "LLY",
    "V", "JNJ", "UNH", "JPM", "WMT", "PG", "MA", "XOM", "CVX", "HD",
    "PFE", "BAC", "KO", "PEP", "ADBE", "CMCSA", "NFLX", "INTC", "CSCO", "VZ",
    "T", "MRK", "ABT", "ORCL", "CRM", "MCD", "COST", "DHR", "MDT", "TXN",
    "NEE", "PM", "BMY", "UNP", "LIN", "UPS", "QCOM", "HON", "NKE", "DIS"
]


STRICTER_WEEKLY_CHANGE_THRESHOLD = 7
STRICTER_VOLUME_THRESHOLD = 70_000_000

import csv
import random  # <--- AÑADE ESTO

def fetch_symbols_from_csv(path="data/symbols.csv"):
    try:
        with open(path, newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            symbols = [row["Symbol"] for row in reader if row.get("Symbol")]
            random.shuffle(symbols)  
            print(f"📄 Se cargaron {len(symbols)} símbolos desde {path}")
            return symbols
    except Exception as e:
        print(f"❌ Error leyendo CSV de símbolos desde '{path}': {e}")
        return local_sp500_symbols



def is_options_enabled(symbol):
    try:
        asset = api.get_asset(symbol)
        return getattr(asset, 'options_enabled', False)
    except:
        return False

# Primero la lista de prioridad, luego el resto (sin duplicados)
stock_assets = priority_symbols + [s for s in fetch_symbols_from_csv() if s not in priority_symbols]



evaluated_symbols_today = set()
last_reset_date = datetime.now().date()
quiver_semaphore = None
quiver_approval_cache = {}

def get_top_signals(verbose=False):
    print("🧩 Entrando en get_top_signals()...")  # 🔍 Diagnóstico
    return run_in_quiver_loop(_get_top_signals_async(verbose))


async def _get_top_signals_async(verbose=False):
    global evaluated_symbols_today, last_reset_date, quiver_semaphore
    if quiver_semaphore is None:
        quiver_semaphore = asyncio.Semaphore(3)

    async def evaluate_symbol(symbol):
        if symbol in quiver_approval_cache:
            approved = quiver_approval_cache[symbol]
            print(f"↩️ [{symbol}] Resultado en caché", flush=True)
            if approved:
                print(f"✅ {symbol} approved.", flush=True)
                return (symbol, 90, "Quiver")
            return None

        print(f"🔎 Checking {symbol}...", flush=True)
        try:
            async with quiver_semaphore:
                approved = await _async_is_approved_by_quiver(symbol)
            quiver_approval_cache[symbol] = approved
            if approved:
                print(f"✅ {symbol} approved.", flush=True)
                return (symbol, 90, "Quiver")
        except Exception as e:
            print(f"⚠️ Error evaluando señales Quiver para {symbol}: {e}")
        return None

    while True:
        today = datetime.now().date()
        if today != last_reset_date:
            evaluated_symbols_today.clear()
            quiver_approval_cache.clear()
            last_reset_date = today
            print("🔁 Reiniciando símbolos evaluados: nuevo día detectado")

        # Refresh positions cache once per cycle
        get_cached_positions(refresh=True)

        symbols_to_evaluate = [
            s for s in stock_assets
            if s not in evaluated_symbols_today and not is_position_open(s)
        ]
        symbols_to_evaluate = symbols_to_evaluate[:100]

        # Si no hay símbolos restantes, comienza una nueva ronda
        if not symbols_to_evaluate:
            evaluated_symbols_today.clear()
            print("🔄 Todos los símbolos analizados. Iniciando nueva ronda.")
            continue

        for s in symbols_to_evaluate:
            evaluated_symbols_today.add(s)

        tasks = [asyncio.create_task(evaluate_symbol(sym)) for sym in symbols_to_evaluate]
        results = []
        for coro in asyncio.as_completed(tasks):
            try:
                r = await coro
            except Exception as e:
                print(f"⚠️ Tarea fallida: {e}")
                continue
            if r:
                results.append(r)
                if len(results) >= 5:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    break
        if results:
            return results[:5]

    return []

def get_top_shorts(min_criteria=20, verbose=False):
    shorts = []
    already_considered = set()

    # Refresh positions cache once before scanning
    get_cached_positions(refresh=True)

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
