import os
import time
import requests
import alpaca_trade_api as tradeapi
import yfinance as yf
from dotenv import load_dotenv
from datetime import datetime, timedelta

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
    
def get_projected_volume_spy():
    try:
        now = datetime.utcnow()
        market_open_time = datetime(now.year, now.month, now.day, 13, 30)
        if now < market_open_time:
            print("⏳ Mercado aún no ha abierto. No se puede proyectar volumen.")
            return 0

        data = yf.download("SPY", period="2d", interval="5m", progress=False)
        if data.empty:
            print("⚠️ No hay datos de SPY, usando volumen medio histórico estimado.")
            return 50_000_000

        minutes_passed = max((now - market_open_time).total_seconds() / 60, 1)
        volume_so_far = data["Volume"].sum()
        projected_volume = volume_so_far / minutes_passed * 390
        return projected_volume
    except Exception as e:
        print(f"❌ Error calculando volumen proyectado: {e}")
        return 0

def is_market_volatile_or_low_volume():
    try:
        vix = yf.download('^VIX', period='5d', interval='1d')
        if vix.empty:
            print("⚠️ No se pudo obtener datos de VIX.")
            return False

        vix_close = vix['Close'].dropna()
        if vix_close.empty:
            print("⚠️ No hay datos suficientes de cierre de VIX.")
            return False

        last_vix = float(vix_close.iloc[-1].item())
        projected_volume = get_projected_volume_spy()
        base_threshold = 30_000_000

        projected_volume = float(projected_volume)  # ya seguro
        print(f"📊 Último VIX: {last_vix:.2f} | Volumen SPY proyectado: {int(projected_volume):,}")


        is_volatile = last_vix > 30
        is_low_volume = projected_volume < base_threshold

        if is_volatile:
            print(f"⚠️ Día muy volátil (VIX {last_vix:.2f} > 30)")
        if is_low_volume:
            print(f"⚠️ Volumen proyectado bajo en SPY ({int(projected_volume):,} < {base_threshold:,})")

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

