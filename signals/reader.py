#reader.py

from signals.filters import (
    is_position_open,
    is_approved_by_finnhub_and_alphavantage,
    get_cached_positions,
    is_approved_by_quiver,
)
from signals.quiver_utils import _async_is_approved_by_quiver, fetch_quiver_signals
from signals.quiver_event_loop import run_in_quiver_loop
import asyncio
from broker.alpaca import api
from signals.scoring import fetch_yfinance_stock_data
from datetime import datetime, timedelta
from utils.logger import log_event
from signals.adaptive_bonus import apply_adaptive_bonus
from signals.fmp_utils import get_fmp_grade_score
from signals.fmp_signals import get_fmp_signal_score
import yfinance as yf
import os
import pandas as pd
import json
from signals.aggregator import WeightedSignalAggregator





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

# Ruta del historial de órdenes para cálculos de bonificación
ORDERS_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
    "orders_history.csv",
)

import csv

def fetch_symbols_from_csv(path="data/symbols.csv"):
    try:
        with open(path, newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            symbols = [row["Symbol"] for row in reader if row.get("Symbol")]
            symbols.sort()
            print(
                f"📄 Se cargaron {len(symbols)} símbolos desde {path} en orden fijo"
            )
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


BLACKLIST_DAYS = 5  # Puede ponerse en config/env si se desea


def is_blacklisted_recent_loser(symbol: str, blacklist_days: int = BLACKLIST_DAYS) -> bool:
    try:
        if not os.path.exists(ORDERS_HISTORY_FILE):
            return False
        df = pd.read_csv(ORDERS_HISTORY_FILE)
        df = df[df["resultado"] == "perdedora"]
        df["fecha_entrada"] = pd.to_datetime(df["fecha_entrada"], errors="coerce")

        cutoff = datetime.now() - timedelta(days=blacklist_days)
        recent_losses = df[
            (df["symbol"] == symbol) & (df["fecha_entrada"] >= cutoff)
        ]

        return not recent_losses.empty  # True = símbolo penalizado
    except Exception as e:
        print(f"⚠️ Error evaluando lista negra de {symbol}: {e}")
        return False  # Permitir si hay error


def has_downtrend(symbol: str, days: int = 4) -> bool:
    try:
        df = yf.download(symbol, period=f"{days}d", interval="1d", progress=False)
        close_data = df["Close"]
        if isinstance(close_data, pd.DataFrame):
            if symbol in close_data.columns:
                close_series = close_data[symbol]
            else:
                close_series = close_data.iloc[:, 0]
        else:
            close_series = close_data
        closes = close_series.dropna().tolist()
        return len(closes) >= 3 and closes[-1] < closes[-2] < closes[-3]
    except Exception as e:
        log_event(f"⚠️ Error obteniendo precios de cierre para {symbol}: {e}")
        return False


def get_trade_history_score(symbol: str, min_trades: int = 2) -> int:
    """Calcula un puntaje de bonificación basado en el historial de operaciones.

    Se suma 1 punto si la tasa de aciertos es >= 60% y otro punto si la
    rentabilidad media estimada es positiva. No penaliza historiales pobres
    ni la falta de datos.
    """
    try:
        if not os.path.exists(ORDERS_HISTORY_FILE):
            return 0
        df = pd.read_csv(ORDERS_HISTORY_FILE)
        df = df[df["resultado"].isin(["ganadora", "perdedora"])]
        symbol_history = df[df["symbol"] == symbol]

        if len(symbol_history) < min_trades:
            return 0

        win_rate = (symbol_history["resultado"] == "ganadora").mean()
        score = 0
        if win_rate >= 0.6:
            score += 1

        def calc_pnl(row):
            if row["tipo"] == "long":
                return (row["precio_salida"] - row["precio_entrada"]) * row["shares"]
            elif row["tipo"] == "short":
                return (row["precio_entrada"] - row["precio_salida"]) * row["shares"]
            return 0

        symbol_history = symbol_history.copy()
        symbol_history["pnl_estimado"] = symbol_history.apply(calc_pnl, axis=1)
        if symbol_history["pnl_estimado"].mean() > 0:
            score += 1

        return score
    except Exception as e:
        print(f"⚠️ Error evaluando historial de {symbol}: {e}")
        return 0


evaluated_symbols_today = set()
last_reset_date = datetime.now().date()
quiver_semaphore = None
quiver_approval_cache = {}

PROGRESS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "evaluated_symbols.json",
)


def _load_evaluated_symbols():
    global evaluated_symbols_today, last_reset_date
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        date_str = data.get("date")
        symbols = data.get("symbols", [])
        if date_str == datetime.now().date().isoformat():
            evaluated_symbols_today = set(symbols)
            last_reset_date = datetime.fromisoformat(date_str).date()
    except Exception:
        pass


def _save_evaluated_symbols():
    try:
        os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": last_reset_date.isoformat(),
                    "symbols": sorted(evaluated_symbols_today),
                },
                f,
            )
    except Exception as e:
        print(f"⚠️ No se pudo guardar progreso de símbolos: {e}")


_load_evaluated_symbols()


def get_top_signals(verbose=False, exclude=None):
    """Return up to five top trading opportunities.

    Parameters
    ----------
    verbose : bool, optional
        If ``True`` debug information is printed.
    exclude : Iterable[str], optional
        Symbols that should be ignored for this scan. Useful to avoid
        re-evaluating tickers that are already pending or have been
        executed earlier in the day.
    """

    print("🧩 Entrando en get_top_signals()...")  # 🔍 Diagnóstico
    return run_in_quiver_loop(_get_top_signals_async(verbose, exclude))


async def _get_top_signals_async(verbose=False, exclude=None):
    global evaluated_symbols_today, last_reset_date, quiver_semaphore
    if quiver_semaphore is None:
        quiver_semaphore = asyncio.Semaphore(3)

    exclude = set(exclude or [])

    aggregator = WeightedSignalAggregator(
        {
            "base": 1,
            "grade": float(os.getenv("FMP_GRADE_WEIGHT", 5)),
            "fmp": float(os.getenv("FMP_SIGNAL_WEIGHT", 2)),
        }
    )

    async def apply_external_scores(symbol, base_score):
        """Combina el score base con calificaciones y señales FMP."""
        scores = {"base": base_score}
        grade_score = await asyncio.to_thread(get_fmp_grade_score, symbol)
        if grade_score is not None:
            scores["grade"] = grade_score
        else:
            print(
                f"⚠️ {symbol} sin calificación FMP, usando score base",
                flush=True,
            )
        fmp_signal = await asyncio.to_thread(get_fmp_signal_score, symbol)
        if fmp_signal is not None:
            scores["fmp"] = fmp_signal
        return aggregator.combine(scores)

    async def evaluate_symbol(symbol):
        if symbol in quiver_approval_cache:
            approved = quiver_approval_cache[symbol]
            print(f"↩️ [{symbol}] Resultado en caché", flush=True)
            if approved:
                print(f"✅ {symbol} approved.", flush=True)
                bonus = get_trade_history_score(symbol)
                if bonus > 0:
                    print(
                        f"✅ {symbol} bonificado con {bonus} puntos por buen historial"
                    )
                final_score = 90 + bonus
                adaptive_bonus = apply_adaptive_bonus(symbol, mode="long")
                final_score += adaptive_bonus
                graded = await apply_external_scores(symbol, final_score)
                if graded is None:
                    return None
                return (symbol, graded, "Quiver")
            return None

        print(f"🔎 Checking {symbol}...", flush=True)
        try:
            async with quiver_semaphore:
                approved = await _async_is_approved_by_quiver(symbol)
            quiver_approval_cache[symbol] = approved
            if approved:
                print(f"✅ {symbol} approved.", flush=True)
                bonus = get_trade_history_score(symbol)
                if bonus > 0:
                    print(
                        f"✅ {symbol} bonificado con {bonus} puntos por buen historial"
                    )
                final_score = 90 + bonus
                adaptive_bonus = apply_adaptive_bonus(symbol, mode="long")
                final_score += adaptive_bonus
                graded = await apply_external_scores(symbol, final_score)
                if graded is None:
                    return None
                return (symbol, graded, "Quiver")
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
            _save_evaluated_symbols()

        # Refresh positions cache once per cycle
        get_cached_positions(refresh=True)

        symbols_to_evaluate = [
            s
            for s in stock_assets
            if s not in evaluated_symbols_today
            and s not in exclude
            and not is_position_open(s)
        ]

        filtered_symbols = []
        for s in symbols_to_evaluate:
            if is_blacklisted_recent_loser(s):
                print(
                    f"🚫 {s} descartado por pérdida reciente (lista negra temporal)"
                )
                continue
            filtered_symbols.append(s)
        symbols_to_evaluate = filtered_symbols[:100]

        # Si no hay símbolos restantes, comienza una nueva ronda
        if not symbols_to_evaluate:
            evaluated_symbols_today.clear()
            _save_evaluated_symbols()
            print("🔄 Todos los símbolos analizados. Iniciando nueva ronda.")
            continue

        for s in symbols_to_evaluate:
            evaluated_symbols_today.add(s)
        _save_evaluated_symbols()

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

def get_top_shorts(min_criteria=20, verbose=False, exclude=None):
    shorts = []
    already_considered = set()
    exclude = set(exclude or [])

    # Refresh positions cache once before scanning
    get_cached_positions(refresh=True)

    for symbol in stock_assets:
        if symbol in already_considered or symbol in exclude or is_position_open(symbol):
            continue
        if is_blacklisted_recent_loser(symbol):
            log_event(
                f"🚫 {symbol} descartado por pérdida reciente (lista negra temporal)"
            )
            continue
        already_considered.add(symbol)

        if is_approved_by_quiver(symbol):
            log_event(f"⛔ {symbol} tiene señales alcistas en Quiver. Short descartado.")
            continue

        try:
            quiver_signals = fetch_quiver_signals(symbol)
            if quiver_signals.get("has_recent_sec13f_activity") or quiver_signals.get("has_recent_sec13f_changes"):
                log_event(f"⛔ {symbol} con señales 13F positivas. Short descartado.")
                continue
        except Exception as e:
            log_event(f"⚠️ Error obteniendo señales 13F para {symbol}: {e}")
            continue

        if not has_downtrend(symbol):
            log_event(f"⛔ {symbol} no cumple patrón bajista de 3 días. Short descartado.")
            continue

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

            symbol_score = get_trade_history_score(symbol)
            if symbol_score > 0:
                print(
                    f"✅ {symbol} bonificado con {symbol_score} puntos por buen historial"
                )
            score += symbol_score

            if verbose:
                print(f"🔻 {symbol}: score={score} (SHORT) → weekly_change={weekly_change}, trend={trend}, price_24h={price_change_24h}")

            if score >= min_criteria and is_approved_by_finnhub_and_alphavantage(symbol):
                adaptive_bonus = apply_adaptive_bonus(symbol, mode="short")
                score += adaptive_bonus
                shorts.append((symbol, score, "Técnico"))
            elif verbose:
                print(f"⛔ {symbol} descartado (short): score={score} o no aprobado por Finnhub/AlphaVantage")

        except Exception as e:
            print(f"❌ Error en short scan {symbol}: {e}")

    if not shorts:
        return []

    shorts.sort(key=lambda x: x[1], reverse=True)
    return shorts[:5]
