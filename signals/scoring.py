import yfinance as yf
from datetime import datetime, timedelta
from config import STRATEGY_VER
import os
import yaml
import pandas as pd
import math
from utils.logger import log_event
from utils.symbols import detect_asset_class, normalize_for_yahoo

_POLICY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "policy.yaml")
with open(_POLICY_PATH, "r", encoding="utf-8") as _f:
    _policy = yaml.safe_load(_f)
SCORE_CFG = _policy.get("score", {})
STRONG_RECENCY_HOURS = float(SCORE_CFG.get("strong_recency_hours", 48))

_CACHE_TTL = timedelta(minutes=5)
_stock_cache = {}


class SkipSymbol(Exception):
    """Signal that a symbol should be skipped by upstream callers."""


class YFPricesMissingError(Exception):
    """Raised when Yahoo Finance does not return enough pricing data."""


def _normalize_0_100(x: float) -> int:
    """Coerce ``x`` into an integer within the [0, 100] range."""
    try:
        return max(0, min(100, int(round(x))))
    except Exception:
        return 0


def _atr_z_penalty(atr_series: list[float], cfg) -> float:
    p = (cfg or {}).get("score", {}).get("atr_z_penalty", {})
    look = int(p.get("lookback_days", 20))
    z_th = float(p.get("z_threshold", 1.0))
    max_pen = float(p.get("max_penalty", 10.0))
    data = atr_series[-look:] if len(atr_series) >= look else atr_series
    if len(data) < 5:
        return 0.0
    import statistics

    mu = statistics.median(data)
    sd = statistics.pstdev(data) or 1e-6
    z = (data[-1] - mu) / sd
    pen = -min(max(0.0, z - z_th), max_pen)
    if pen < 0:
        log_event(f"PENALTY ATR_Z z≈{z:.2f} -> {pen}")
    return pen


def _gap_open_rejection_penalty(
    open_price: float,
    first_n_min_prices: list[float],
    prev_close: float,
    cfg,
) -> float:
    g = (cfg or {}).get("score", {}).get("gap_open_rejection", {})
    look = int(g.get("lookback_minutes", 15))
    weak_th = float(g.get("weakness_threshold_pct", -0.3))
    pen = float(g.get("penalty", 5.0))
    if prev_close <= 0 or open_price <= 0:
        return 0.0
    gap_pct = 100.0 * (open_price / prev_close - 1.0)
    if gap_pct <= 0.0:
        return 0.0
    if not first_n_min_prices:
        return 0.0
    low_early = min(first_n_min_prices[:look])
    weakness_pct = 100.0 * (low_early / open_price - 1.0)
    penalty = -pen if weakness_pct <= weak_th else 0.0
    if penalty < 0:
        log_event(
            f"PENALTY GAP_REJECTION gap={gap_pct:.2f}% weak={weakness_pct:.2f}% -> {penalty}"
        )
    return penalty


def _recency_boost(days_since_event: float | None, strong_recency_hours: float, k: float = 2.0, decay: float = 0.1) -> float:
    """
    Devuelve multiplicador de recencia.
    - Si edad_horas <= strong_recency_hours -> boost fijo k
    - Si mayor: decae exponencial: exp(-decay * (edad_horas - strong_recency_hours)/24)
    """
    if days_since_event is None:
        return 1.0
    edad_h = float(days_since_event) * 24.0
    if edad_h <= strong_recency_hours:
        return k
    extra_h = max(0.0, edad_h - strong_recency_hours)
    return float(math.exp(-decay * (extra_h / 24.0)))


def fetch_yfinance_stock_data(symbol, verbose: bool = False, return_history: bool = False):
    now = datetime.utcnow()
    cached = _stock_cache.get(symbol)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        if return_history:
            return cached["data"], cached.get("history")
        return cached["data"]
    try:
        asset_class = detect_asset_class(symbol)
        yf_symbol = normalize_for_yahoo(symbol) if asset_class == "preferred" else symbol
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info
        market_cap = info.get("marketCap")
        volume = info.get("volume")
        hist = ticker.history(period="90d", interval="1d")
        if hist.empty or hist["Close"].dropna().empty:
            raise YFPricesMissingError("history_empty")
        weekly_change = None
        if len(hist) >= 2:
            lookback = min(len(hist) - 1, 6)
            base_idx = -lookback - 1
            base_price = hist['Close'].iloc[base_idx]
            if base_price:
                weekly_change = (
                    (hist['Close'].iloc[-1] - base_price) / base_price
                ) * 100
        trend_positive = hist['Close'].iloc[-1] > hist['Close'].iloc[0] if len(hist) >= 2 else None
        price_change_24h = (
            abs((hist['Close'].iloc[-1] - hist['Close'].iloc[-2]) / hist['Close'].iloc[-2]) * 100
            if len(hist) >= 2
            else None
        )
        volume_7d_avg = hist['Volume'].mean() if not hist['Volume'].isna().all() else None

        current_price = hist['Close'].iloc[-1] if not hist.empty else None
        if current_price is None or (isinstance(current_price, float) and math.isnan(current_price)):
            raise YFPricesMissingError("last_close_missing")
        atr = None
        try:
            if len(hist) >= 2 and {"High", "Low", "Close"}.issubset(hist.columns):
                high = hist['High']
                low = hist['Low']
                close = hist['Close']
                prev_close = close.shift(1)
                tr = pd.concat([
                    high - low,
                    (high - prev_close).abs(),
                    (low - prev_close).abs(),
                ], axis=1).max(axis=1)
                atr = tr.rolling(14).mean().iloc[-1]
        except Exception:
            atr = None

        data = (
            market_cap,
            volume,
            weekly_change,
            trend_positive,
            price_change_24h,
            volume_7d_avg,
            current_price,
            atr,
        )
        _stock_cache[symbol] = {"data": data, "history": hist, "ts": now}
        if return_history:
            return data, hist
        return data
    except SkipSymbol:
        raise
    except Exception:
        data = (None, None, None, None, None, None, None, None)
        if return_history:
            return data, None
        return data


def score_long_signal(symbol: str, market_data: dict) -> dict:
    """Return a normalized 0-100 score for ``symbol`` based on ``market_data``."""
    components = {}
    score = 0
    quiver = market_data.get("quiver", {})
    quiver_score = 0
    strong_count = 0
    quiver_recent = False

    def _decay(base, days):
        return base if days <= 3 else max(5, int(base * (0.9 ** days)))

    def _add_signal(key: str, base: int, strong: bool = False):
        nonlocal quiver_score, strong_count, quiver_recent
        if key not in quiver:
            return
        days = quiver.get(key)
        if days is None:
            return
        weight = _decay(base, days)
        boost = _recency_boost(days, STRONG_RECENCY_HOURS)
        weight *= boost
        quiver_score += weight
        age_h = days * 24.0
        if age_h <= STRONG_RECENCY_HOURS:
            log_event(
                f"SCORE {symbol}: recency boost k=2.0 age={age_h:.1f}h (≤ {STRONG_RECENCY_HOURS}h)"
            )
        else:
            log_event(
                f"SCORE {symbol}: decayed boost={boost:.2f} age={age_h:.1f}h (> {STRONG_RECENCY_HOURS}h)"
            )
        if strong:
            strong_count += 1
            if age_h <= STRONG_RECENCY_HOURS:
                quiver_recent = True

    _add_signal("insiders", 30, strong=True)
    _add_signal("gov_contract", 25, strong=True)
    _add_signal("patent_momentum", 15, strong=True)
    _add_signal("sec13f_activity", 5)
    _add_signal("sec13f_changes", 8)
    _add_signal("house", 10)
    quiver_score += min(3, quiver.get("wsb", 0))
    quiver_score += min(3, quiver.get("twitter", 0))
    components["quiver"] = quiver_score
    if strong_count >= 2:
        components["quiver_double_strong"] = True

    fmp = market_data.get("fmp", {})
    rs = fmp.get("ratings_snapshot", 0)
    components["ratings_snapshot"] = min(max(rs, 0), 10)
    score += components["ratings_snapshot"]
    rsi = fmp.get("rsi")
    if rsi is not None:
        rsi_score = 5 if rsi < 30 else -5 if rsi > 70 else 0
        components["rsi"] = rsi_score
        score += rsi_score
    news = fmp.get("news_polarity", 0)
    components["news_polarity"] = max(min(news, 5), -5)
    score += components["news_polarity"]
    if score > 20:
        score = 20
    score += quiver_score

    penalties = 0.0
    atr_series20 = market_data.get("atr_series20", []) or []
    open_price = market_data.get("open_price", 0.0)
    prev_close = market_data.get("prev_close", 0.0)
    first_15m_prices = market_data.get("first_15m_prices", []) or []

    penalties += _atr_z_penalty(atr_series20, _policy)
    penalties += _gap_open_rejection_penalty(open_price, first_15m_prices, prev_close, _policy)

    macro = market_data.get("macro_vix", 0)
    if macro and macro > 0:
        penalties -= 5

    score += penalties
    components["penalties"] = penalties

    score = _normalize_0_100(score)
    return {"score": score, "components": components, "quiver_recent": quiver_recent}
