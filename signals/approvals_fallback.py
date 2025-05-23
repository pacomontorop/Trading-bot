from filters import is_approved_by_finnhub, is_approved_by_alphavantage

def is_approved_by_finnhub_and_alphavantage(symbol):
    approved_finnhub = is_approved_by_finnhub(symbol)
    approved_alpha = is_approved_by_alphavantage(symbol)

    if not (approved_alpha and approved_finnhub):
        print(f"⛔ {symbol} no aprobado: Finnhub={approved_finnhub}, AlphaVantage={approved_alpha}")

    return approved_alpha and approved_finnhub
