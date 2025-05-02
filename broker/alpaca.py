import os
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
from utils/logger import log_event

load_dotenv()
api = tradeapi.REST(
    os.getenv(\"APCA_API_KEY_ID\"),
    os.getenv(\"APCA_API_SECRET_KEY\"),
    \"https://paper-api.alpaca.markets\",
    api_version='v2'
)

def is_market_open():
    try:
        clock = api.get_clock()
        return clock.is_open
    except Exception as e:
        log_event(f\"❌ Error checking market open: {e}\")
        return False

def get_current_price(symbol):
    try:
        bars = api.get_bars(symbol, tradeapi.TimeFrame.Minute, limit=1)
        if not bars.df.empty:
            return bars.df['close'].iloc[0]
    except Exception as e:
        log_event(f\"❌ Error fetching price for {symbol}: {e}\")
    return None

