#filters.py

import os
import time
import requests
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
from signals.quiver_approval import is_approved_by_quiver

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
        for p in positions:
            if p.symbol == symbol:
                return True
        return False
    except Exception as e:
        print(f"❌ Error verificando posición abierta para {symbol}: {e}")
        return True

def confirm_secondary_indicators(symbol):
    return True

def has_negative_news(symbol):
    return False

def is_approved_by_finnhub(symbol):
    try:
        FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
        url_rating = f"https://finnhub.io/api/v1/stock/recommendation?symbol={symbol}&token={FINNHUB_KEY}"
        url_news = f"https://finnhub.io/api/v1/news-sentiment?symbol={symbol}&token={FINNHUB_KEY}"

        r_rating = requests.get(url_rating, timeout=5).json()
        time.sleep(1)
        r_news = requests.get(url_news, timeout=5).json()
        time.sleep(1)

        if r_rating and r_rating[0]['strongBuy'] + r_rating[0]['buy'] >= r_rating[0]['sell'] + r_rating[0]['strongSell']:
            sentiment_score = r_news.get("sentiment", {}).get("companyNewsScore", 0)
            if sentiment_score >= 0:
                return True
    except Exception as e:
        print(f"⚠️ Finnhub error for {symbol}: {e}")
    return False

def is_approved_by_alphavantage(symbol):
    try:
        AV_KEY = os.getenv("ALPHA_VANTAGE_API_KEY")
        url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers={symbol}&apikey={AV_KEY}"
        r = requests.get(url, timeout=5).json()

        if not r or "feed" not in r:
            print(f"⚠️ Alpha Vantage: no hay feed de noticias para {symbol}")
            return False

        sentiment_sum = sum([
            1 if article.get("overall_sentiment_label", "").lower() == "positive" else
            -1 if article.get("overall_sentiment_label", "").lower() == "negative" else 0
            for article in r["feed"]
        ])

        return sentiment_sum >= 0
    except Exception as e:
        print(f"⚠️ Alpha Vantage error for {symbol}: {e}")
        return False

def is_approved_by_finnhub_and_alphavantage(symbol):
    approved_finnhub = is_approved_by_finnhub(symbol)
    approved_alpha = True

    try:
        ALPHA_KEY = os.getenv("ALPHA_VANTAGE_API_KEY")
        url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers={symbol}&apikey={ALPHA_KEY}"
        r = requests.get(url, timeout=5).json()
        feed = r.get("feed", [])

        if feed:
            sentiment_scores = [
                item.get("overall_sentiment_score", 0)
                for item in feed if "overall_sentiment_score" in item
            ]
            avg_sentiment = sum(sentiment_scores) / len(sentiment_scores) if sentiment_scores else 0
            approved_alpha = avg_sentiment >= 0
        else:
            print(f"⚠️ Alpha Vantage: no hay feed de noticias para {symbol} (lo consideramos aprobado)")
            approved_alpha = True

    except Exception as e:
        print(f"⚠️ Alpha Vantage error para {symbol}: {e} (lo consideramos aprobado)")
        approved_alpha = True

    if not (approved_alpha and approved_finnhub):
        print(f"⛔ {symbol} no aprobado: Finnhub={approved_finnhub}, AlphaVantage={approved_alpha}")

    return approved_alpha and approved_finnhub

def is_symbol_approved(symbol):
    """
    Función principal de aprobación: prioriza Quiver.
    Si Quiver aprueba, se compra. Si no, se requiere aprobación conjunta Finnhub + Alpha.
    """
    if is_approved_by_quiver(symbol):
        print(f"✅ {symbol} aprobado por Quiver")
        return True
    return is_approved_by_finnhub_and_alphavantage(symbol)

