# utils/generate_symbols_csv.py

import os
import pandas as pd
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

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
        _ETF_PATTERNS = (" ETF", " Fund", "Warrant", " Index Fund")

        def _is_junk(asset) -> bool:
            name = (asset.name or "").strip()
            sym = (asset.symbol or "").strip().upper()
            # Warrants (symbol suffix W/WS/WT and name contains Warrant)
            if any(pattern in name for pattern in _ETF_PATTERNS):
                return True
            # Warrant-style suffixes: symbols ending in W, WS, WT with len >= 5
            if len(sym) >= 5 and sym.endswith(("W", "WS", "WT", "WW")):
                return True
            # SPAC units ending in U with len >= 5
            if len(sym) >= 5 and sym.endswith("U") and sym[:-1].isalpha():
                return True
            # Rights symbols ending in R with len >= 5
            if len(sym) >= 5 and sym.endswith("R") and sym[:-1].isalpha():
                return True
            return False

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
            if a.tradable
            and a.exchange in ["NASDAQ", "NYSE"]
            and getattr(a, "asset_class", "us_equity") == "us_equity"
            and not _is_junk(a)
        ]
        df = pd.DataFrame(symbols)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        df.to_csv(output_path, index=False)
        print(f"✅ CSV generado con {len(df)} símbolos en {output_path}")
    except Exception as e:
        print(f"❌ Error generando CSV de símbolos: {e}")

if __name__ == "__main__":
    generate_symbols_csv()

