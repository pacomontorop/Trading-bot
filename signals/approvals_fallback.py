from signals.finnhub import get_finnhub_recommendation, get_finnhub_sentiment
from signals.alphavantage import get_alphavantage_recommendation

def is_approved_by_finnhub_and_alphavantage(symbol):
    try:
        finnhub_rec = get_finnhub_recommendation(symbol)
        finnhub_sent = get_finnhub_sentiment(symbol)
        alpha_rec = get_alphavantage_recommendation(symbol)

        return (
            finnhub_rec in {"buy", "strong_buy"} and
            finnhub_sent != "bearish" and
            alpha_rec in {"buy", "strong_buy"}
        )
    except Exception as e:
        print(f"⚠️ Error en fallback Finnhub + Alpha para {symbol}: {e}")
        return False
