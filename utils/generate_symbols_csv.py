# utils/generate_symbols_csv.py

import os
import pandas as pd
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
import csv

load_dotenv()

api = tradeapi.REST(
    os.getenv("APCA_API_KEY_ID"),
    os.getenv("APCA_API_SECRET_KEY"),
    "https://paper-api.alpaca.markets",
    api_version='v2'
)

def generate_symbols_csv(output_path="data/symbols.csv"):
    try:
        assets = api.list_assets(status="active")
        symbols = [
            {
                "Symbol": a.symbol,
                "Name": a.name,
                "Exchange": a.exchange,
                "Tradable": a.tradable,
                "Shortable": a.shortable,
                "Marginable": a.marginable
            }
            for a in assets
            if a.tradable and a.exchange in ["NASDAQ", "NYSE"]
        ]
        df = pd.DataFrame(symbols)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        df.to_csv(output_path, index=False)
        print(f"✅ CSV generado con {len(df)} símbolos en {output_path}")
    except Exception as e:
        print(f"❌ Error generando CSV de símbolos: {e}")

if __name__ == "__main__":
    generate_symbols_csv()

