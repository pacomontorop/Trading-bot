# grade_news.py
"""Monitor FMP grade news for rating changes and place small trades."""
import time
from broker.alpaca import is_market_open, get_current_price
from core.executor import place_order_with_trailing_stop, place_short_order_with_trailing_buy
from signals.reader import stock_assets
from signals.fmp_utils import grades_news

_processed = {}


def scan_grade_changes():
    print("📰 grade_news_scan iniciado.", flush=True)
    while True:
        if is_market_open():
            for symbol in stock_assets:
                try:
                    data = grades_news(symbol, limit=1)
                    if not data:
                        continue
                    item = data[0]
                    pub_date = item.get("publishedDate")
                    if _processed.get(symbol) == pub_date:
                        continue
                    _processed[symbol] = pub_date
                    new_grade = (item.get("newGrade") or "").lower()
                    prev_grade = (item.get("previousGrade") or "").lower()
                    if new_grade == prev_grade or not new_grade or not prev_grade:
                        continue
                    price = get_current_price(symbol)
                    if not price:
                        continue
                    amount = price if price > 10 else 10
                    if new_grade == "buy" and prev_grade in ("hold", "sell"):
                        place_order_with_trailing_stop(symbol, amount)
                    elif new_grade == "sell" and prev_grade in ("hold", "buy"):
                        place_short_order_with_trailing_buy(symbol, amount)
                except Exception as e:
                    print(f"⚠️ Error procesando grade news de {symbol}: {e}")
            time.sleep(300)
        else:
            time.sleep(60)
