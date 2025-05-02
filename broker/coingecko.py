
import requests

# Convert Alpaca crypto symbol to CoinGecko ID
def get_coingecko_id_from_symbol(symbol):
    known = {
        "BTC": "bitcoin", "ETH": "ethereum", "DOGE": "dogecoin", "SOL": "solana",
        "ADA": "cardano", "AVAX": "avalanche-2", "UNI": "uniswap", "LINK": "chainlink",
        "DOT": "polkadot", "MATIC": "matic-network", "LTC": "litecoin", "SHIB": "shiba-inu",
        "XRP": "ripple", "USDT": "tether", "USDC": "usd-coin", "BCH": "bitcoin-cash",
        "AAVE": "aave", "SUSHI": "sushi", "TRX": "tron", "GRT": "the-graph", 
        "MKR": "maker", "YFI": "yearn-finance", "PEPE": "pepe", "XTZ": "tezos"
    }

    base_symbol = symbol.split("/")[0].upper()
    return known.get(base_symbol)

# Fetch market data for crypto using CoinGecko
def fetch_coingecko_crypto_data(symbol):
    try:
        coingecko_id = get_coingecko_id_from_symbol(symbol)
        if not coingecko_id:
            print(f"⚠️ No CoinGecko ID para {symbol}")
            return (None,) * 6

        url = f"https://api.coingecko.com/api/v3/coins/{coingecko_id}"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            print(f"❌ Error {response.status_code} obteniendo {symbol} desde CoinGecko")
            return (None,) * 6

        data = response.json()
        market_data = data.get('market_data', {})
        market_cap = market_data.get('market_cap', {}).get('usd')
        volume = market_data.get('total_volume', {}).get('usd')
        weekly_change = market_data.get('price_change_percentage_7d')
        trend = market_data.get('price_change_percentage_24h', 0) > 0
        price_change_24h = abs(market_data.get('price_change_percentage_24h', 0))
        volume_7d_avg = volume / 7 if volume else None

        return market_cap, volume, weekly_change, trend, price_change_24h, volume_7d_avg
    except Exception as e:
        print(f"❌ Error fetching {symbol} from CoinGecko: {e}")
        return (None,) * 6
