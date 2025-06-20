#filters.py

import os
import time
import requests
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
from signals.quiver_utils import is_approved_by_quiver

load_dotenv()
api = tradeapi.REST(
    os.getenv("APCA_API_KEY_ID"),
    os.getenv("APCA_API_SECRET_KEY"),
    "https://paper-api.alpaca.markets",
    api_version='v2'
)

def is_position_open(symbol):
    try:
        positions = api.list_positions()
        return any(p.symbol == symbol for p in positions)
    except Exception as e:
        print(f"❌ Error verificando posición abierta para {symbol}: {e}")
        return True

def confirm_secondary_indicators(symbol):
    return True

def has_negative_news(symbol):
    return False

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
    if not (finnhub and alpha):
        print(f"⛔ {symbol} no aprobado: Finnhub={finnhub}, AlphaVantage={alpha}")
    return finnhub and alpha

def is_symbol_approved(symbol):
    print(f"\n🚦 Iniciando análisis de aprobación para {symbol}...")

    if is_approved_by_quiver(symbol):
        print(f"✅ {symbol} aprobado por Quiver")
        return True

    print(f"➡️ {symbol} no pasó filtro Quiver. Evaluando Finnhub y AlphaVantage...")
    approved = is_approved_by_finnhub_and_alphavantage(symbol)
    if approved:
        print(f"✅ {symbol} aprobado por Finnhub + AlphaVantage")
    return approved
