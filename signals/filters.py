import os
import time
import requests
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

load_dotenv()

# Inicializa conexión con Alpaca
try:
    api = tradeapi.REST(
        os.getenv("APCA_API_KEY_ID"),
        os.getenv("APCA_API_SECRET_KEY"),
        "https://paper-api.alpaca.markets",
        api_version='v2'
    )
except Exception as e:
    print(f"❌ Error inicializando API de Alpaca: {e}")
    api = None

# Verifica si una posición está abierta
def is_position_open(symbol):
    try:
        if not api:
            print("⚠️ API de Alpaca no disponible.")
            return False
        positions = api.list_positions()
        return any(p.symbol == symbol for p in positions)
    except Exception as e:
        print(f"❌ Error verificando posición abierta para {symbol}: {e}")
        return False

# Por ahora siempre retorna True, placeholder para más validaciones
def confirm_secondary_indicators(symbol):
    return True

# Por ahora siempre retorna False, placeholder para detección de noticias negativas
def has_negative_news(symbol):
    return False

# Revisión de Finnhub (rating + sentimiento)
def is_approved_by_finnhub(symbol):
    FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
    if not FINNHUB_KEY:
        print("⚠️ Clave API de Finnhub no definida.")
        return False

    try:
        url_rating = f"https://finnhub.io/api/v1/stock/recommendation?symbol={symbol}&token={FINNHUB_KEY}"
        url_news = f"https://finnhub.io/api/v1/news-sentiment?symbol={symbol}&token={FINNHUB_KEY}"

        r_rating = requests.get(url_rating, timeout=5).json()
        time.sleep(1)
        r_news = requests.get(url_news, timeout=5).json()
        time.sleep(1)

        if r_rating and isinstance(r_rating, list) and r_rating[0]:
            rating = r_rating[0]
            if rating['strongBuy'] + rating['buy'] >= rating['sell'] + rating['strongSell']:
                sentiment_score = r_news.get("sentiment", {}).get("companyNewsScore", 0)
                return sentiment_score >= 0
    except Exception as e:
        print(f"⚠️ Finnhub error para {symbol}: {e}")

    return False

# Revisión de Alpha Vantage (sentimiento de noticias)
def is_approved_by_alphavantage(symbol):
    AV_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    if not AV_KEY:
        print("⚠️ Clave API de Alpha Vantage no definida.")
        return False

    try:
        url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers={symbol}&apikey={AV_KEY}"
        r = requests.get(url, timeout=5).json()

        feed = r.get("feed", [])
        if not feed:
            print(f"⚠️ Alpha Vantage: no hay feed de noticias para {symbol}")
            return True  # ← si no hay datos, no bloqueamos

        sentiment_sum = sum([
            1 if article.get("overall_sentiment_label", "").lower() == "positive" else
           -1 if article.get("overall_sentiment_label", "").lower() == "negative" else 0
            for article in feed
        ])

        return sentiment_sum >= 0
    except Exception as e:
        print(f"⚠️ Alpha Vantage error para {symbol}: {e}")
        return True  # ← en caso de error, consideramos aprobado

# Evaluación combinada (Finnhub + Alpha Vantage)
def is_approved_by_finnhub_and_alphavantage(symbol):
    approved_finnhub = is_approved_by_finnhub(symbol)
    approved_alpha = is_approved_by_alphavantage(symbol)

    if not (approved_finnhub and approved_alpha):
        print(f"⛔ {symbol} no aprobado: Finnhub={approved_finnhub}, AlphaVantage={approved_alpha}")

    return approved_finnhub and approved_alpha


