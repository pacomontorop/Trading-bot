import requests
import os
from signals.quiver_utils import is_approved_by_quiver, get_all_quiver_signals

API_KEY = os.getenv("QUIVER_API_KEY")  # o directamente: API_KEY = "TU_API_KEY"
headers = {"x-api-key": API_KEY}
symbol = "AAPL"

endpoints = {
    "Insider Trading": f"https://api.quiverquant.com/beta/historical/insidertrading/{symbol}",
    "Government Contracts": f"https://api.quiverquant.com/beta/historical/governmentcontracts/{symbol}",
    "Patents": f"https://api.quiverquant.com/beta/historical/patents/{symbol}",
    "Social Sentiment (WSB)": f"https://api.quiverquant.com/beta/historical/wsb/{symbol}",
    "ETF Flows": f"https://api.quiverquant.com/beta/historical/etfflow/{symbol}",
    "13F Filings": f"https://api.quiverquant.com/beta/historical/sec13f/{symbol}",
    "Dark Pool": f"https://api.quiverquant.com/beta/historical/darkpool/{symbol}",
    "Political Beta": f"https://api.quiverquant.com/beta/historical/politicalbeta/{symbol}",
    "Twitter Sentiment": f"https://api.quiverquant.com/beta/historical/twitter/{symbol}",
    "App Ratings": f"https://api.quiverquant.com/beta/historical/apprating/{symbol}",
}

for name, url in endpoints.items():
    try:
        print(f"\n🔍 Checking {name} for {symbol}...")
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            data = r.json()
            print(f"✅ {name} returned {len(data)} records.")
            if data:
                print(f"   📄 First item: {data[0]}")
            else:
                print("   ⚠️ No data found.")
        else:
            print(f"❌ {name} failed. Status: {r.status_code}. Response: {r.text}")
    except Exception as e:
        print(f"❌ Exception fetching {name}: {e}")
