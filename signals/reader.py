#reader.py

from collections import Counter
import json
import math
from signals.filters import (
    is_position_open,
    is_approved_by_finnhub_and_alphavantage,
    get_cached_positions,
)
import re
import sys
try:
    from signals.quiver_utils import (
        _async_is_approved_by_quiver,
        fetch_quiver_signals,
        is_approved_by_quiver,
    )
    import signals.quiver_utils as _quiver_utils
except ModuleNotFoundError:
    _quiver_utils = sys.modules.get("signals.quiver_utils")
    if _quiver_utils is None:
        raise
    _async_is_approved_by_quiver = getattr(
        _quiver_utils, "_async_is_approved_by_quiver", None
    )
    fetch_quiver_signals = getattr(_quiver_utils, "fetch_quiver_signals", None)
    is_approved_by_quiver = getattr(_quiver_utils, "is_approved_by_quiver", None)

QUIVER_APPROVAL_THRESHOLD = getattr(_quiver_utils, "QUIVER_APPROVAL_THRESHOLD", 5)
has_recent_quiver_event = getattr(
    _quiver_utils, "has_recent_quiver_event", lambda *a, **k: False
)
from signals.quiver_event_loop import run_in_quiver_loop
import asyncio
from broker.alpaca import api
try:
    from signals.scoring import (
        fetch_yfinance_stock_data,
        SkipSymbol,
        YFPricesMissingError,
    )
except ImportError:
    from signals.scoring import fetch_yfinance_stock_data

    class SkipSymbol(Exception):
        pass

    class YFPricesMissingError(Exception):
        pass
from datetime import datetime, timedelta
try:
    from utils.logger import log_event
except ModuleNotFoundError:
    _utils_logger = sys.modules.get("utils.logger")
    if _utils_logger is None:
        log_event = lambda *a, **k: None  # type: ignore
    else:
        log_event = getattr(_utils_logger, "log_event", lambda *a, **k: None)
try:
    from utils.state import mark_evaluated
except ModuleNotFoundError:
    _utils_state = sys.modules.get("utils.state")
    if _utils_state is None:
        mark_evaluated = lambda *a, **k: None  # type: ignore
    else:
        mark_evaluated = getattr(_utils_state, "mark_evaluated", lambda *a, **k: None)
try:
    from utils import metrics
except ModuleNotFoundError:
    _utils_pkg = sys.modules.get("utils")
    if _utils_pkg is None:
        class _Metrics:
            @staticmethod
            def inc(*args, **kwargs):
                return None

        metrics = _Metrics()  # type: ignore
    else:
        metrics = getattr(_utils_pkg, "metrics", None)
        if metrics is None:
            class _Metrics:
                @staticmethod
                def inc(*args, **kwargs):
                    return None

            metrics = _Metrics()  # type: ignore
from signals.adaptive_bonus import apply_adaptive_bonus
from signals.fmp_utils import get_fmp_grade_score
from signals.fmp_signals import get_fmp_signal_score
from libs.logging.approvals import approvals_log
import yfinance as yf
import os
import pandas as pd
import json
from signals.aggregator import WeightedSignalAggregator
import random
import config
from utils.symbols import detect_asset_class, normalize_for_yahoo
from utils.health import record_scan


def maybe_fetch_externals(symbol, prelim_score, cfg):
    th = int(((cfg or {}).get("cache", {}) or {}).get("score_recalc_threshold", 60))
    if prelim_score < th:
        log_event(
            f"SCORE {symbol}: skip externals (prelim={prelim_score}<{th})"
        )
        return None
    return True





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


def _only_equities(symbols):
    return [s for s in symbols if detect_asset_class(s) == "equity"]


priority_symbols = _only_equities(priority_symbols)


def _compile_exclude_patterns():
    patterns = []
    gate_cfg = (getattr(config, "_policy", {}) or {}).get("gate", {})
    for expr in gate_cfg.get("exclude_regex", []) or []:
        try:
            patterns.append(re.compile(expr))
        except re.error:
            log_event(
                f"SCAN: patrón inválido en exclude_regex: {expr}",
                event="SCAN",
            )
    return patterns


_EXCLUDE_PATTERNS = _compile_exclude_patterns()


def is_symbol_excluded(symbol: str) -> bool:
    upper = symbol or ""
    return any(p.search(upper) for p in _EXCLUDE_PATTERNS)


priority_symbols = [s for s in priority_symbols if not is_symbol_excluded(s)]


STRICTER_WEEKLY_CHANGE_THRESHOLD = 7
STRICTER_VOLUME_THRESHOLD = 70_000_000
RELAX_BEAR_PATTERN = os.getenv("RELAX_BEAR_PATTERN", "true").lower() in {"1", "true", "yes"}

# Ruta del historial de órdenes para cálculos de bonificación
ORDERS_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
    "orders_history.csv",
)

import csv

def fetch_symbols_from_csv(path="data/symbols.csv"):
    stats = Counter()
    symbols: list[str] = []
    seen: set[str] = set()
    try:
        with open(path, newline="") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                raw_symbol = (row.get("Symbol") or "").strip().upper()
                if not raw_symbol or raw_symbol in seen:
                    continue
                seen.add(raw_symbol)
                name = (row.get("Name") or "").upper()
                if "ETF" in name:
                    stats["etf_filter"] += 1
                    continue
                if detect_asset_class(raw_symbol) != "equity":
                    stats["non_equity"] += 1
                    continue
                symbols.append(raw_symbol)
        random.shuffle(symbols)
        print(
            f"📄 Se cargaron {len(symbols)} símbolos desde {path} en orden aleatorio"
        )
        return symbols, stats
    except Exception as e:
        print(f"❌ Error leyendo CSV de símbolos desde '{path}': {e}")
        random.shuffle(local_sp500_symbols)
        return local_sp500_symbols, stats



def is_options_enabled(symbol):
    try:
        asset = api.get_asset(symbol)
        return getattr(asset, 'options_enabled', False)
    except:
        return False

def _format_exclusions(counter: Counter) -> str:
    ordered = {k: int(counter.get(k, 0)) for k in sorted(counter.keys())}
    return json.dumps(ordered, separators=(",", ":"))


_csv_symbols, _csv_stats = fetch_symbols_from_csv()
UNIVERSE_EXCLUDED = Counter(
    {"yahoo_missing": 0, "price_nan": 0, "penny_stock": 0, "etf_filter": 0}
)
if _csv_stats:
    UNIVERSE_EXCLUDED.update({"etf_filter": _csv_stats.get("etf_filter", 0)})

# Combina símbolos de prioridad con el resto en orden aleatorio sin duplicados
stock_assets = priority_symbols + [
    s
    for s in _csv_symbols
    if s not in priority_symbols
    and detect_asset_class(s) == "equity"
    and not is_symbol_excluded(s)
]
random.shuffle(stock_assets)

log_event(
    f"UNIVERSE equities size={len(stock_assets)} excluded={_format_exclusions(UNIVERSE_EXCLUDED)}",
    event="SCAN",
)


BLACKLIST_DAYS = 5  # Puede ponerse en config/env si se desea


def _extract_close_series(history, symbol: str) -> pd.Series:
    if history is None or getattr(history, "empty", True):
        return pd.Series(dtype=float)
    try:
        close_data = history["Close"]
    except Exception:
        return pd.Series(dtype=float)
    if isinstance(close_data, pd.DataFrame):
        if symbol in close_data.columns:
            series = close_data[symbol]
        else:
            series = close_data.iloc[:, 0]
    else:
        series = close_data
    return series.dropna()


def _compute_rsi(close_series: pd.Series, period: int = 14) -> float | None:
    close_series = close_series.dropna()
    if close_series.size <= period:
        return None
    delta = close_series.diff()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = avg_loss.replace(0, pd.NA)
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    value = rsi.iloc[-1]
    if pd.isna(value):
        return None
    return float(value)


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


def has_downtrend(symbol: str, days: int = 4, close_series: pd.Series | None = None) -> bool:
    try:
        if close_series is None or close_series.empty:
            history = None
            data = fetch_yfinance_stock_data(symbol, return_history=True)
            if isinstance(data, tuple) and len(data) == 2:
                _, history = data
            if history is None:
                asset_class = detect_asset_class(symbol)
                if asset_class != "equity":
                    return False
                yf_symbol = normalize_for_yahoo(symbol) if asset_class == "preferred" else symbol
                history = yf.download(yf_symbol, period=f"{max(days, 20)}d", interval="1d", progress=False)
            close_series = _extract_close_series(history, symbol)
        else:
            close_series = close_series.dropna()

        if close_series.empty:
            return False

        pct_neg = close_series.pct_change().tail(3).lt(0).sum()
        ema20 = close_series.ewm(span=20, adjust=False).mean().iloc[-1]
        last_close = close_series.iloc[-1]
        if pd.isna(ema20):
            return False
        return bool(int(pct_neg) >= 2 and float(last_close) < float(ema20))
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


def reset_symbol_rotation():
    """Shuffle the symbol universe and clear evaluation progress."""
    random.shuffle(stock_assets)
    evaluated_symbols_today.clear()
    _save_evaluated_symbols()


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
    global evaluated_symbols_today, last_reset_date, quiver_semaphore, UNIVERSE_EXCLUDED
    if quiver_semaphore is None:
        cache_cfg = (getattr(config, "_policy", {}) or {}).get("cache", {})
        concurrency = int(cache_cfg.get("quiver_concurrency", 2))
        quiver_semaphore = asyncio.Semaphore(max(1, concurrency))

    exclude = set(exclude or [])

    log_event(
        f"UNIVERSE equities size={len(stock_assets)} excluded={_format_exclusions(UNIVERSE_EXCLUDED)}",
        event="SCAN",
    )

    aggregator = WeightedSignalAggregator(
        {
            "base": 1,
            "grade": float(os.getenv("FMP_GRADE_WEIGHT", 5)),
            "fmp": float(os.getenv("FMP_SIGNAL_WEIGHT", 2)),
        }
    )

    async def apply_external_scores(symbol, base_score):
        """Combina el score base con calificaciones y señales FMP."""
        if maybe_fetch_externals(symbol, base_score, config._policy) is None:
            return base_score
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
        cache_hit = symbol in quiver_approval_cache
        metrics.inc("scanned")
        log_event(
            "cached evaluation" if cache_hit else "evaluating candidate",
            event="SCAN",
            symbol=symbol,
            cached=cache_hit,
        )

        if cache_hit:
            approved = quiver_approval_cache[symbol]
            print(f"↩️ [{symbol}] Resultado en caché", flush=True)
            cond = (
                approved
                and approved.get("active_signals")
                and approved.get("score", 0) >= QUIVER_APPROVAL_THRESHOLD
                and approved.get("fresh")
            )
            if cond:
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
                try:
                    data = await asyncio.to_thread(fetch_yfinance_stock_data, symbol)
                except SkipSymbol as exc:
                    log_event(
                        f"SCORE {symbol}: skip symbol ({exc})",
                        event="SCORE",
                        symbol=symbol,
                    )
                    return None
                except YFPricesMissingError as exc:
                    UNIVERSE_EXCLUDED["yahoo_missing"] += 1
                    log_event(
                        f"SCAN {symbol}: datos YF incompletos ({exc})",
                        event="SCAN",
                        symbol=symbol,
                    )
                    mark_evaluated(symbol)
                    return None
                current_price = data[6] if data and len(data) >= 8 else None
                atr = data[7] if data and len(data) >= 8 else None
                try:
                    price_val = float(current_price) if current_price is not None else None
                except Exception:
                    price_val = None
                if price_val is None or math.isnan(price_val) or price_val <= 0:
                    UNIVERSE_EXCLUDED["price_nan"] += 1
                    log_event(
                        f"SCAN {symbol}: precio inválido ({current_price})",
                        event="SCAN",
                        symbol=symbol,
                    )
                    return None
                if price_val < 5.0:
                    UNIVERSE_EXCLUDED["penny_stock"] += 1
                    log_event(
                        f"SCAN {symbol}: descartado por penny_stock (price={price_val:.2f})",
                        event="SCAN",
                        symbol=symbol,
                    )
                    return None
                current_price = price_val
                metrics.inc("scored")
                log_event(
                    f"score={graded:.1f} source=Quiver",
                    event="SCORE",
                    symbol=symbol,
                )
                return (symbol, graded, "Quiver", current_price, atr)
            return None

        print(f"🔎 Checking {symbol}...", flush=True)
        try:
            async with quiver_semaphore:
                approved = await _async_is_approved_by_quiver(symbol)
            if isinstance(approved, dict):
                approved["fresh"] = await asyncio.to_thread(
                    has_recent_quiver_event, symbol, days=2
                )
            quiver_approval_cache[symbol] = approved
            cond = (
                approved
                and approved.get("active_signals")
                and approved.get("score", 0) >= QUIVER_APPROVAL_THRESHOLD
                and approved.get("fresh")
            )
            if cond:
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
                try:
                    data = await asyncio.to_thread(fetch_yfinance_stock_data, symbol)
                except SkipSymbol as exc:
                    log_event(
                        f"SCORE {symbol}: skip symbol ({exc})",
                        event="SCORE",
                        symbol=symbol,
                    )
                    return None
                except YFPricesMissingError as exc:
                    UNIVERSE_EXCLUDED["yahoo_missing"] += 1
                    log_event(
                        f"SCAN {symbol}: datos YF incompletos ({exc})",
                        event="SCAN",
                        symbol=symbol,
                    )
                    mark_evaluated(symbol)
                    return None
                current_price = data[6] if data and len(data) >= 8 else None
                atr = data[7] if data and len(data) >= 8 else None
                try:
                    price_val = float(current_price) if current_price is not None else None
                except Exception:
                    price_val = None
                if price_val is None or math.isnan(price_val) or price_val <= 0:
                    UNIVERSE_EXCLUDED["price_nan"] += 1
                    log_event(
                        f"SCAN {symbol}: precio inválido ({current_price})",
                        event="SCAN",
                        symbol=symbol,
                    )
                    return None
                if price_val < 5.0:
                    UNIVERSE_EXCLUDED["penny_stock"] += 1
                    log_event(
                        f"SCAN {symbol}: descartado por penny_stock (price={price_val:.2f})",
                        event="SCAN",
                        symbol=symbol,
                    )
                    return None
                current_price = price_val
                metrics.inc("scored")
                log_event(
                    f"score={graded:.1f} source=Quiver",
                    event="SCORE",
                    symbol=symbol,
                )
                return (symbol, graded, "Quiver", current_price, atr)
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
        random.shuffle(symbols_to_evaluate)

        record_scan("equity", len(symbols_to_evaluate))

        if not symbols_to_evaluate:
            print("🔄 Todos los símbolos evaluados. Reiniciando ciclo.")
            reset_symbol_rotation()
            await asyncio.sleep(60)
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
    global UNIVERSE_EXCLUDED
    shorts = []
    already_considered = set()
    exclude = set(exclude or [])

    log_event(
        f"UNIVERSE equities size={len(stock_assets)} excluded={_format_exclusions(UNIVERSE_EXCLUDED)}",
        event="SCAN",
    )

    # Refresh positions cache once before scanning
    get_cached_positions(refresh=True)

    for symbol in stock_assets:
        if symbol in already_considered or symbol in exclude or is_position_open(symbol):
            continue
        if detect_asset_class(symbol) != "equity":
            continue
        metrics.inc("scanned")
        record_scan("equity")
        signals_count = 0
        if is_blacklisted_recent_loser(symbol):
            log_event(
                f"🚫 {symbol} descartado por pérdida reciente (lista negra temporal)"
            )
            approvals_log(
                symbol,
                "REJECT",
                "recent_loser_blacklist",
                score=None,
                signals_active=signals_count,
                side="SHORT",
                module="short_scanner",
            )
            continue
        already_considered.add(symbol)

        try:
            quiver_signals = fetch_quiver_signals(symbol)
        except Exception as e:
            log_event(f"⚠️ Error obteniendo señales 13F para {symbol}: {e}")
            approvals_log(
                symbol,
                "REJECT",
                "quiver_fetch_error",
                score=None,
                signals_active=signals_count,
                side="SHORT",
                module="short_scanner",
            )
            continue

        def _is_active_signal(value):
            if isinstance(value, dict):
                return value.get("active", False)
            return getattr(value, "active", bool(value))

        def _signal_days(value):
            if isinstance(value, dict):
                return value.get("days")
            return getattr(value, "days", None)

        bullish_signals = {k for k, v in quiver_signals.items() if _is_active_signal(v)}
        signals_count = len(bullish_signals)

        def _is_strong_signal(name, days):
            if name == "insider_buy_more_than_sell":
                return True
            if name == "has_recent_sec13f_activity":
                return days is not None and days <= 7
            if name == "bullish_price_target":
                return days is not None and days <= 7
            return False

        strong_hits = sum(
            1
            for key, value in quiver_signals.items()
            if _is_active_signal(value) and _is_strong_signal(key, _signal_days(value))
        )

        quiver_blocks = signals_count >= 2 or strong_hits >= 1

        try:
            data, price_history = fetch_yfinance_stock_data(symbol, return_history=True)
        except SkipSymbol as exc:
            log_event(
                f"SCORE {symbol}: skip symbol ({exc})",
                event="SCORE",
                symbol=symbol,
            )
            approvals_log(
                symbol,
                "REJECT",
                "skip_symbol",
                score=None,
                signals_active=signals_count,
                side="SHORT",
                module="short_scanner",
            )
            continue
        except YFPricesMissingError as exc:
            UNIVERSE_EXCLUDED["yahoo_missing"] += 1
            log_event(
                f"SCAN {symbol}: datos YF incompletos ({exc})",
                event="SCAN",
                symbol=symbol,
            )
            continue
        except Exception as e:
            print(f"❌ Error en short scan {symbol}: {e}")
            approvals_log(
                symbol,
                "REJECT",
                "data_error",
                score=None,
                signals_active=signals_count,
                side="SHORT",
                module="short_scanner",
            )
            continue

        if not isinstance(data, tuple) or len(data) < 8:
            if verbose:
                print(f"⚠️ Datos incompletos para {symbol}. Se omite.")
            approvals_log(
                symbol,
                "REJECT",
                "insufficient_data",
                score=None,
                signals_active=signals_count,
                side="SHORT",
                module="short_scanner",
            )
            continue

        (
            market_cap,
            volume,
            weekly_change,
            trend,
            price_change_24h,
            volume_7d_avg,
            current_price,
            atr,
        ) = data

        if price_history is None or getattr(price_history, "empty", True):
            if verbose:
                print(f"⚠️ Historial insuficiente para {symbol}. Se omite.")
            approvals_log(
                symbol,
                "REJECT",
                "insufficient_history",
                score=None,
                signals_active=signals_count,
                side="SHORT",
                module="short_scanner",
            )
            continue

        close_series = _extract_close_series(price_history, symbol)
        if close_series.empty or close_series.size < 3:
            if verbose:
                print(f"⚠️ Historial insuficiente para {symbol}. Se omite.")
            approvals_log(
                symbol,
                "REJECT",
                "insufficient_history",
                score=None,
                signals_active=signals_count,
                side="SHORT",
                module="short_scanner",
            )
            continue

        ema20 = close_series.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = close_series.ewm(span=50, adjust=False).mean().iloc[-1]
        last_close = close_series.iloc[-1]
        try:
            price_val = float(last_close)
        except Exception:
            price_val = None
        if price_val is None or math.isnan(price_val) or price_val <= 0:
            UNIVERSE_EXCLUDED["price_nan"] += 1
            log_event(
                f"SCAN {symbol}: precio de cierre inválido ({last_close})",
                event="SCAN",
                symbol=symbol,
            )
            continue
        if price_val < 5.0:
            UNIVERSE_EXCLUDED["penny_stock"] += 1
            log_event(
                f"SCAN {symbol}: descartado por penny_stock (price={price_val:.2f})",
                event="SCAN",
                symbol=symbol,
            )
            continue
        changes = close_series.pct_change()
        window = 4 if RELAX_BEAR_PATTERN else 3
        recent = changes.tail(window)
        recent_valid = recent.dropna()
        down_days = int(recent_valid.tail(3).lt(0).sum())
        missing = window - len(recent_valid.tail(3))
        price_below_ema20 = not pd.isna(ema20) and last_close < ema20
        if RELAX_BEAR_PATTERN:
            down_ok = down_days >= 2 and price_below_ema20 and missing <= 1
        else:
            down_ok = down_days >= 2 and price_below_ema20 and missing == 0

        rsi14 = _compute_rsi(close_series)
        ema_stack = (
            not pd.isna(ema20)
            and not pd.isna(ema50)
            and last_close < ema20 < ema50
        )
        momentum_override = (
            ema_stack
            and (rsi14 is not None and rsi14 > 30)
            and (weekly_change is not None and weekly_change <= -3)
        )

        if not ((down_ok and not quiver_blocks) or momentum_override):
            if quiver_blocks:
                log_event(
                    f"⛔ {symbol} bloqueado por Quiver (señales={signals_count}, fuertes={strong_hits})."
                )
                approvals_log(
                    symbol,
                    "REJECT",
                    "quiver_block",
                    score=None,
                    signals_active=signals_count,
                    side="SHORT",
                    module="short_scanner",
                )
            else:
                log_event(
                    f"⛔ {symbol} sin patrón bajista (últimos3_down={down_days}, precio<EMA20={price_below_ema20})."
                )
                approvals_log(
                    symbol,
                    "REJECT",
                    "no_downtrend",
                    score=None,
                    signals_active=signals_count,
                    side="SHORT",
                    module="short_scanner",
                )
            continue

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

        metrics.inc("scored")

        if verbose:
            print(
                f"🔻 {symbol}: score={score} (SHORT) → weekly_change={weekly_change}, trend={trend}, price_24h={price_change_24h}"
            )

        log_score = score
        if score >= min_criteria and is_approved_by_finnhub_and_alphavantage(symbol):
            adaptive_bonus = apply_adaptive_bonus(symbol, mode="short")
            score += adaptive_bonus
            shorts.append((symbol, score, "Técnico", current_price, atr))
            metrics.inc("approved")
            approvals_log(
                symbol,
                "APPROVE",
                "momentum_override" if momentum_override else "all_checks_passed",
                score=score,
                signals_active=signals_count,
                side="SHORT",
                module="short_scanner",
            )
        else:
            if verbose:
                print(f"⛔ {symbol} descartado (short): score={score} o no aprobado por Finnhub/AlphaVantage")
            reason = "external_checks_failed" if log_score >= min_criteria else "score_threshold"
            approvals_log(
                symbol,
                "REJECT",
                reason,
                score=score,
                signals_active=signals_count,
                side="SHORT",
                module="short_scanner",
            )

    if not shorts:
        return []

    shorts.sort(key=lambda x: x[1], reverse=True)
    return shorts[:5]
