#filters.py

import os
import time
from datetime import datetime, timedelta

import requests
import pandas as pd
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

from data.tiingo_client import get_daily_prices
from data.fred_client import get_macro_snapshot
from signals.quiver_utils import is_approved_by_quiver
from signals.reddit_scraper import get_reddit_sentiment
from utils.daily_set import DailySet
from utils.logger import log_dir
import yfinance as yf
from signals.fmp_utils import search_stock_news

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


def has_negative_news(symbol):
    """Use FMP news sentiment; block if negatives outweigh positives by ≥2."""
    try:
        news = search_stock_news(symbol)
        pos = sum(1 for n in news if n.get("sentiment") == "positive")
        neg = sum(1 for n in news if n.get("sentiment") == "negative")
        return (neg - pos) >= 2
    except Exception:
        return False


def macro_score() -> float:
    """Return a macroeconomic score based on FRED data.

    Positive values indicate a favorable environment, negative values a headwind.
    """
    score = 0.0
    try:
        snapshot = get_macro_snapshot()
        inflation = snapshot.get("inflation")
        unemployment = snapshot.get("unemployment")
        if inflation is not None:
            score += (2 - inflation) / 10  # reward low inflation, penalize high
        if unemployment is not None:
            score += (5 - unemployment) / 10  # reward low unemployment
    except Exception as e:
        print(f"⚠️ FRED macro check failed: {e}")
    return score


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

def is_symbol_approved(symbol):
    print(f"\n🚦 Iniciando análisis de aprobación para {symbol}...")
    score = 0.0
    score += macro_score()
    score -= volatility_penalty(symbol)
    score += reddit_score(symbol)

    had_external_approval = False
    if is_approved_by_quiver(symbol):
        print(f"✅ {symbol} aprobado por Quiver")
        score += 1.0
        had_external_approval = True
    else:
        print(f"➡️ {symbol} no pasó filtro Quiver. Evaluando Finnhub y AlphaVantage...")
        if is_approved_by_finnhub_and_alphavantage(symbol):
            print(f"✅ {symbol} aprobado por Finnhub + AlphaVantage")
            score += 0.5
            had_external_approval = True
        elif is_approved_by_fmp(symbol):
            score += 0.25
            had_external_approval = True

    print(f"📈 Score final {symbol}: {score:.2f}")
    approved = score > 0 and had_external_approval
    if approved:
        approved_symbols_today.add(symbol)
    else:
        rejected_symbols_today.add(symbol)
    try:
        os.makedirs(log_dir, exist_ok=True)
        status = "APPROVED" if approved else "REJECTED"
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(log_dir, "approvals.log"), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {symbol} {status}\n")
    except Exception:
        pass
    return approved


def is_approved_by_fmp(symbol):
    try:
        from signals.fmp_utils import get_fmp_grade_score
        threshold = float(os.getenv("FMP_GRADE_THRESHOLD", 0))
        score = get_fmp_grade_score(symbol)
        return score is not None and score >= threshold
    except Exception as e:
        print(f"⚠️ FMP error for {symbol}: {e}")
    return False
