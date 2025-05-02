import os
import time
import requests
import alpaca_trade_api as tradeapi
import yfinance as yf
from dotenv import load_dotenv

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
        return True  # Por seguridad asumimos que está abierta
      
def confirm_secondary_indicators(symbol):
    return True

def has_negative_news(symbol):
    return False

def is_market_volatile_or_low_volume():
    try:
        # Descargar varios días para asegurar datos útiles
        vix = yf.download('^VIX', period='5d', interval='1d')
        spy = yf.Ticker("SPY")
        hist = spy.history(period="5d")

        if vix.empty or hist.empty:
            print("⚠️ No se pudo obtener datos de VIX o SPY.")
            return False

        # Tomar el último dato con volumen válido
        vix_close = vix['Close'].dropna()
        spy_volume = hist['Volume'].dropna()

        if vix_close.empty or spy_volume.empty:
            print("⚠️ No hay datos suficientes de cierre o volumen.")
            return False

        last_vix = float(vix_close.iloc[-1].item())
        last_spy_volume = int(spy_volume.iloc[-1])

        base_threshold = 10_000_000

        print(f"📊 Último VIX: {last_vix:.2f} | Último volumen SPY: {last_spy_volume:,}")

        is_volatile = last_vix > 30
        is_low_volume = last_spy_volume < base_threshold

        if is_volatile:
            print(f"⚠️ Día muy volátil (VIX {last_vix:.2f} > 30)")
        if is_low_volume:
            print(f"⚠️ Volumen bajo en SPY ({last_spy_volume:,} < {base_threshold:,})")

        return is_volatile or is_low_volume

    except Exception as e:
        print(f"⚠️ Error checking volatility/volume: {e}")
        return False

def is_approved_by_finnhub(symbol):
    try:
        FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
        url_rating = f"https://finnhub.io/api/v1/stock/recommendation?symbol={symbol}&token={FINNHUB_KEY}"
        url_news = f"https://finnhub.io/api/v1/news-sentiment?symbol={symbol}&token={FINNHUB_KEY}"

        r_rating = requests.get(url_rating, timeout=5).json()
        time.sleep(1)  # ← Pausa para evitar rate limit
        r_news = requests.get(url_news, timeout=5).json()
        time.sleep(1)  # ← Otra pausa (si quieres más seguridad)

        if r_rating and r_rating[0]['strongBuy'] + r_rating[0]['buy'] >= r_rating[0]['sell'] + r_rating[0]['strongSell']:
            sentiment_score = r_news.get("sentiment", {}).get("companyNewsScore", 0)
            if sentiment_score >= 0:
                return True
    except Exception as e:
        print(f"⚠️ Finnhub error for {symbol}: {e}")
    return False

