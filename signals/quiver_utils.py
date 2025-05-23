#quiver_utils.py

def get_quiver_signals(symbol):
    """
    Devuelve un diccionario con las señales de Quiver para un símbolo.
    Ideal para logging, scoring, backtesting y decisión desacoplada.
    """
    signals = {
        "insider_buy_more_than_sell": False,
        "has_gov_contract": False,
        "positive_patent_momentum": False,
        "trending_wsb": False,
        "bullish_etf_flow": False
    }

    try:
        # Insider trading
        url = f"{QUIVER_BASE_URL}/live/insidertrading/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list):
            total_buy = sum(1 for tx in data if tx.get("Transaction") == "Purchase")
            total_sell = sum(1 for tx in data if tx.get("Transaction") == "Sale")
            signals["insider_buy_more_than_sell"] = total_buy > total_sell

        # Gov contracts
        url = f"{QUIVER_BASE_URL}/live/govcontracts/{symbol}"
        data = safe_quiver_request(url)
        signals["has_gov_contract"] = isinstance(data, list) and len(data) > 0

        # Patent momentum
        url = f"{QUIVER_BASE_URL}/live/patentmomentum/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list) and len(data) > 0:
            signals["positive_patent_momentum"] = data[0].get("Momentum", 0) > 0

        # WSB mentions
        url = f"{QUIVER_BASE_URL}/live/wsb/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list) and len(data) > 0:
            signals["trending_wsb"] = data[0].get("Mentions", 0) > 10

        # ETF flow
        url = f"{QUIVER_BASE_URL}/live/etf/{symbol}"
        data = safe_quiver_request(url)
        if isinstance(data, list) and len(data) > 0:
            signals["bullish_etf_flow"] = data[0].get("NetFlow", 0) > 0

    except Exception as e:
        print(f"⚠️ Error obteniendo señales Quiver para {symbol}: {e}")

    return signals
