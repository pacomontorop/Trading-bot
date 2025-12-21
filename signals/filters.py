#filters.py

import os
import time
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

from data.tiingo_client import get_daily_prices
from signals.reddit_scraper import get_reddit_sentiment
from utils.daily_set import DailySet
from utils.logger import log_event
from utils import metrics
from typing import Optional, Dict, Any
import yfinance as yf

load_dotenv()
api = tradeapi.REST(
    os.getenv("APCA_API_KEY_ID"),
    os.getenv("APCA_API_SECRET_KEY"),
    "https://paper-api.alpaca.markets",
    api_version='v2'
)

# Daily tracking of approval outcomes
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
APPROVED_FILE = os.path.join(DATA_DIR, "approved_symbols.json")
REJECTED_FILE = os.path.join(DATA_DIR, "rejected_symbols.json")
approved_symbols_today = DailySet(APPROVED_FILE)
rejected_symbols_today = DailySet(REJECTED_FILE)

# Cache for list_positions results to reduce API calls
_POSITIONS_CACHE = {"timestamp": 0.0, "data": []}

FMP_API_KEY = os.getenv("FMP_API_KEY") or os.getenv("FINANCIAL_MODELING_PREP_API_KEY")


def macro_score():
    """Compatibilidad retro: el ajuste macro ahora se aplica fuera de este módulo."""
    return 0.0

# --- Listas de keywords (pueden ampliarse) ---
NEGATIVE_KEYWORDS = {
    "lawsuit","lawsuits","probe","investigation","regulatory probe","fraud","fraudulent",
    "accounting issue","accounting issues","sec charges","charges","indicted","indictment",
    "short seller","short-seller","downgrade","downgraded","cut to sell","downgrade to sell",
    "cuts outlook","cut outlook","weak guidance","guidance cut","profit warning",
    "misses","miss","missed estimates","eps miss","earnings miss","revenue miss","underperform",
    "recall","product recall","data breach","breach","cyberattack","hack","hacked","ransomware",
    "class action","class-action","fire","explosion","accident","fatalities","casualties",
    "layoffs","job cuts","workforce reduction","restructuring charges","impairment",
    "bankruptcy","insolvency","default","chapter 11","chapter 7",
    "sanction","sanctions","fine","fined","penalty","penalties","settlement",
    "antitrust","anti trust","monopoly","antimonopoly","cartel","price fixing",
    "delist","delisting","going concern","going-concern doubt","restatement","restated earnings",
    "resign","resigns","resignation","steps down","suspension",
    "scandal","controversy","whistleblower","whistle-blower","allegations","allegation",
    "sell-off","selloff","collapse","plunge","plunges","tumbles","slump","freefall","meltdown",
    "fall sharply","volatile drop","liquidity crunch","covenant breach",
    "criminal investigation","securities fraud","market manipulation","audit committee review",
    "regulatory action","fined by","probe into","under investigation"
}

POSITIVE_KEYWORDS = {
    "upgrade","upgraded","initiated buy","reiterate buy","overweight","outperform",
    "price target increase","raises target","target raised",
    "raises outlook","raise outlook","guidance raise","raises guidance","strong guidance",
    "beats","beat","surprise beat","tops estimates","exceeds estimates","eps beat","revenue beat",
    "contract win","wins contract","award","awarded","order win","large order","backlog record",
    "approval","regulatory approval","fda approval","sec approval","clearance","authorized",
    "buyback","share repurchase","dividend increase","hike dividend","special dividend",
    "record revenue","record profit","record backlog","all-time high","record high",
    "positive preliminary","prelim beats","reaffirm guidance",
    "partnership","strategic alliance","joint venture","JV","collaboration",
    "expansion","capacity expansion","new plant","new facility","hiring","adds jobs",
    "growth","accelerates","acceleration","momentum","strong demand","resilient demand",
    "innovation","new product","launch","rollout","AI breakthrough","patent granted",
    "positive data","phase 3 success","fast track approval","accelerated approval",
    "solid earnings","robust earnings","margin expansion","operating leverage",
    "deleveraging","debt reduction","investment grade upgrade","credit upgrade"
}


def get_cached_positions(ttl=60, refresh=False):
    """Return cached positions, refreshing if stale or on demand."""
    now = time.time()
    if refresh or now - _POSITIONS_CACHE["timestamp"] > ttl:
        try:
            _POSITIONS_CACHE["data"] = api.list_positions()
        except Exception as e:
            print(f"❌ Error obteniendo posiciones: {e}")
            _POSITIONS_CACHE["data"] = []
        _POSITIONS_CACHE["timestamp"] = now
    return _POSITIONS_CACHE["data"]

def is_position_open(symbol):
    try:
        positions = get_cached_positions()
        return any(p.symbol == symbol for p in positions)
    except Exception as e:
        print(f"❌ Error verificando posición abierta para {symbol}: {e}")
        return True

def _compute_rsi(series, window: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = -delta.clip(upper=0).rolling(window).mean()
    rs = gain / loss
    rsi = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0


def confirm_secondary_indicators(symbol):
    """Basic technical confirmation: trend and momentum checks."""
    try:
        hist = yf.download(symbol, period="200d", interval="1d", progress=False)
        close = hist["Close"].dropna()
        if close.empty:
            return False
        price = close.iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]
        sma200 = close.rolling(200).mean().iloc[-1]
        sma7 = close.rolling(7).mean().iloc[-1]
        cond1 = price >= sma50 and sma50 >= sma200
        cond2 = abs(price - sma7) / sma7 < 0.025
        rsi = _compute_rsi(close)
        cond3 = rsi < 80
        return sum([cond1, cond2, cond3]) >= 2
    except Exception:
        return False


def _label_from_keywords(text: str) -> int:
    """
    +1 si hay términos positivos y no negativos,
    -1 si hay negativos y no positivos,
     0 si ambos o ninguno.
    """
    if not text:
        return 0
    t = text.lower()
    has_neg = any(k in t for k in NEGATIVE_KEYWORDS)
    has_pos = any(k in t for k in POSITIVE_KEYWORDS)
    if has_neg and not has_pos:
        return -1
    if has_pos and not has_neg:
        return +1
    return 0


def _fetch_fmp_stock_news(symbol: str, limit: int = 50, days_back: int = 2):
    """
    Devuelve artículos recientes de FMP para 'symbol' limitando la ventana a 'days_back' días.
    Endpoint: /stable/news/stock?symbols=...&from=YYYY-MM-DD&to=YYYY-MM-DD&page=0&limit=...
    En error devuelve [] (tolerante).
    """
    if not FMP_API_KEY:
        return []
    try:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days_back)
        url = (
            "https://financialmodelingprep.com/stable/news/stock"
            f"?symbols={symbol}"
            f"&from={start_dt:%Y-%m-%d}"
            f"&to={end_dt:%Y-%m-%d}"
            f"&page=0&limit={limit}"
            f"&apikey={FMP_API_KEY}"
        )
        r = requests.get(url, timeout=6)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return []
        # Filtro adicional por fecha exacta
        cutoff = end_dt - timedelta(days=days_back)
        recent = []
        for it in data:
            try:
                d = datetime.fromisoformat((it.get("publishedDate") or "").replace(" ", "T"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                if d >= cutoff:
                    recent.append(it)
            except Exception:
                continue
        return recent
    except Exception:
        return []


def has_negative_news(symbol: str, days_back: int = 2) -> bool:
    """
    Bloquea si las noticias de los últimos 'days_back' días tienen
    (negativas - positivas) >= 2. En error de red/parseo → False (no bloquea).
    """
    try:
        items = _fetch_fmp_stock_news(symbol, limit=50, days_back=days_back)
        pos = neg = 0
        for it in items:
            title = it.get("title") or ""
            text = it.get("text") or ""
            label = _label_from_keywords(f"{title}\n{text}")
            if label > 0:
                pos += 1
            elif label < 0:
                neg += 1
        log_event(
            "negative_news_check",
            symbol=symbol,
            positives=pos,
            negatives=neg,
            lookback_days=days_back,
            articles=len(items)
        )
        return (neg - pos) >= 2
    except Exception as e:
        log_event("negative_news_error", symbol=symbol, error=str(e))
        return False


def reddit_score(symbol: str) -> float:
    """Fetch Reddit sentiment score for ``symbol`` (-1 to 1)."""
    try:
        score = get_reddit_sentiment(symbol)
        print(f"🧾 Reddit score {symbol}: {score:.2f}")
        return score
    except Exception as e:
        print(f"⚠️ Reddit sentiment check failed for {symbol}: {e}")
    return 0.0


def volatility_penalty(symbol: str, lookback: int = 20, threshold: float = 0.05) -> float:
    """Return a penalty for high volatility based on Tiingo data."""
    try:
        end = datetime.utcnow().date()
        start = end - timedelta(days=lookback * 2)
        prices = get_daily_prices(symbol, start_date=start.isoformat(), end_date=end.isoformat())
        df = pd.DataFrame(prices)
        if df.empty or "close" not in df:
            return 0.0
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["return"] = df["close"].pct_change()
        vol = df["return"].std()
        if pd.isna(vol):
            return 0.0
        penalty = max(0.0, vol - threshold)
        if penalty > 0:
            print(f"⚠️ Volatilidad de {symbol}: {vol:.2%} (penalización {penalty:.2%})")
        return penalty
    except Exception as e:
        print(f"⚠️ Tiingo volatility check failed for {symbol}: {e}")
    return 0.0

def is_approved_by_finnhub(symbol):
    try:
        key = os.getenv("FINNHUB_API_KEY")
        r1 = requests.get(f"https://finnhub.io/api/v1/stock/recommendation?symbol={symbol}&token={key}", timeout=5).json()
        time.sleep(1)
        r2 = requests.get(f"https://finnhub.io/api/v1/news-sentiment?symbol={symbol}&token={key}", timeout=5).json()
        time.sleep(1)

        if r1 and r1[0]['strongBuy'] + r1[0]['buy'] >= r1[0]['sell'] + r1[0]['strongSell']:
            return r2.get("sentiment", {}).get("companyNewsScore", 0) >= 0
    except Exception as e:
        print(f"⚠️ Finnhub error for {symbol}: {e}")
    return False

def is_approved_by_alphavantage(symbol):
    try:
        key = os.getenv("ALPHA_VANTAGE_API_KEY")
        r = requests.get(f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers={symbol}&apikey={key}", timeout=5).json()
        if not r or "feed" not in r:
            print(f"⚠️ Alpha Vantage: no hay feed para {symbol}")
            return False
        score = sum(
            1 if a.get("overall_sentiment_label", "").lower() == "positive"
            else -1 if a.get("overall_sentiment_label", "").lower() == "negative"
            else 0 for a in r["feed"]
        )
        return score >= 0
    except Exception as e:
        print(f"⚠️ Alpha Vantage error for {symbol}: {e}")
        return False

def is_approved_by_finnhub_and_alphavantage(symbol):
    finnhub = is_approved_by_finnhub(symbol)
    try:
        alpha = is_approved_by_alphavantage(symbol)
    except Exception as e:
        print(f"⚠️ Alpha fallback error for {symbol}: {e}")
        alpha = True
    if finnhub and alpha:
        return True
    fmp = is_approved_by_fmp(symbol)
    if fmp:
        print(f"✅ {symbol} aprobado por FMP")
    else:
        print(f"⛔ {symbol} no aprobado: Finnhub={finnhub}, AlphaVantage={alpha}, FMP={fmp}")
    return fmp

# Cache for final approval decisions (symbol -> (approved, timestamp, detail))
_APPROVAL_CACHE: Dict[str, tuple[bool, float, Dict[str, Any]]] = {}


def _now_ts() -> float:
    return time.time()


def _is_quiver_strong(symbol: str, cfg) -> bool:
    from signals.quiver_utils import (
        get_all_quiver_signals,
        score_quiver_signals,
        has_recent_quiver_event,
    )

    s = get_all_quiver_signals(symbol)
    q_score = score_quiver_signals(s)
    rec_h = cfg.get("approvals", {}).get("quiver_strong", {}).get("recency_hours", 48)
    score_th = cfg.get("approvals", {}).get("quiver_strong", {}).get("score_threshold", 8.0)
    require_recent = (
        cfg.get("approvals", {}).get("quiver_strong", {}).get("require_recent_event", True)
    )
    fresh = True
    if require_recent:
        fresh = has_recent_quiver_event(symbol, days=max(1, rec_h / 24.0))
    return (q_score >= float(score_th)) and fresh


def _provider_votes(symbol: str, cfg) -> Dict[str, Optional[bool]]:
    """Devuelve votos de {Quiver, FinnhubAlpha, FMP} sin lanzar excepciones."""
    votes: Dict[str, Optional[bool]] = {"Quiver": False, "FinnhubAlpha": False, "FMP": False}
    try:
        from signals.quiver_utils import (
            QUIVER_API_KEY,
            get_all_quiver_signals,
            score_quiver_signals,
            has_recent_quiver_event,
        )

        if not QUIVER_API_KEY:
            votes["Quiver"] = None
        else:
            signals = get_all_quiver_signals(symbol)
            q_score = score_quiver_signals(signals)
            votes["Quiver"] = (q_score >= 5.0) and has_recent_quiver_event(symbol, days=2)
    except Exception:
        votes["Quiver"] = False

    try:
        fh_key = os.getenv("FINNHUB_API_KEY")
        av_key = os.getenv("ALPHA_VANTAGE_API_KEY")
        if not fh_key or not av_key:
            votes["FinnhubAlpha"] = None
        else:
            fh = is_approved_by_finnhub(symbol)
            av = is_approved_by_alphavantage(symbol)
            votes["FinnhubAlpha"] = fh and av
    except Exception:
        votes["FinnhubAlpha"] = False

    try:
        from signals.fmp_signals import get_fmp_signal_score
        if not FMP_API_KEY:
            votes["FMP"] = None
            return votes

        f = get_fmp_signal_score(symbol)
        votes["FMP"] = bool(
            f and isinstance(f.get("score"), (int, float)) and f["score"] > 0
        )
    except Exception:
        votes["FMP"] = False

    return votes


def is_symbol_approved(symbol: str, overall_score: int, cfg) -> bool:
    """Aprobación final: override Quiver opcional o consenso 2/3."""
    ttl = float(((cfg or {}).get("cache", {}) or {}).get("approval_ttl_sec", 300.0))
    cached = _APPROVAL_CACHE.get(symbol)
    now = _now_ts()
    if cached and now - cached[1] < ttl:
        metrics.inc("approved" if cached[0] else "rejected")
        return cached[0]

    try:
        if cfg.get("approvals", {}).get("quiver_override", True):
            if _is_quiver_strong(symbol, cfg):
                log_event(
                    f"APPROVAL {symbol}: ✅ Quiver OVERRIDE (strong & fresh). overall_score={overall_score}"
                )
                metrics.inc("approved")
                _APPROVAL_CACHE[symbol] = (True, now, {"mode": "override"})
                return True

        votes = _provider_votes(symbol, cfg)
        available_votes = {k: v for k, v in votes.items() if v is not None}
        available = len(available_votes)
        if available == 0:
            log_event(
                f"APPROVAL {symbol}: ⚠️ sin proveedores activos; se permite operar. overall_score={overall_score}"
            )
            metrics.inc("approved")
            _APPROVAL_CACHE[symbol] = (True, now, {"mode": "no_providers"})
            return True
        yes = sum(1 for v in available_votes.values() if v)
        needed = min(
            int(cfg.get("approvals", {}).get("consensus_required", 2)),
            available,
        )
        ok = yes >= needed
        detail = ", ".join(
            [f"{k}:{'✓' if v else '×'}" for k, v in available_votes.items()]
        )
        if ok:
            log_event(
                f"APPROVAL {symbol}: ✅ Consenso {yes}/3 → {detail}. overall_score={overall_score}"
            )
            metrics.inc("approved")
        else:
            log_event(
                f"APPROVAL {symbol}: ❌ Consenso {yes}/3 → {detail}. overall_score={overall_score}"
            )
            metrics.inc("rejected")
        _APPROVAL_CACHE[symbol] = (ok, now, votes)
        return ok
    except Exception as e:
        log_event(f"APPROVAL {symbol}: ⛔ error {e}")
        metrics.inc("rejected")
        _APPROVAL_CACHE[symbol] = (False, now, {"error": str(e)})
        return False


def is_approved_by_fmp(symbol):
    try:
        from signals.fmp_utils import get_fmp_grade_score

        threshold = float(os.getenv("FMP_GRADE_THRESHOLD", 0))
        score = get_fmp_grade_score(symbol)
        return score is not None and score >= threshold
    except Exception as e:
        print(f"⚠️ FMP error for {symbol}: {e}")
    return False
