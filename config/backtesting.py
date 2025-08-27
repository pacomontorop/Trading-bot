import argparse
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

from signals.filters import is_approved_by_finnhub_and_alphavantage
from signals.scoring import fetch_yfinance_stock_data
from signals.reader import stock_assets

INITIAL_BALANCE = 100_000
TRAIL_PERCENT = 2.0
MIN_CRITERIA = 5
START_DATE = datetime(2023, 1, 1)
END_DATE = datetime(2024, 12, 31)


def simulate_trade(symbol, hist, verbose=False):
    if hist is None or hist.empty or len(hist) < 10:
        return None

    entry_price = hist['Close'].iloc[0]
    highest_price = entry_price
    trailing_stop = entry_price * (1 - TRAIL_PERCENT / 100)

    for price in hist['Close']:
        if price > highest_price:
            highest_price = price
            trailing_stop = highest_price * (1 - TRAIL_PERCENT / 100)

        if price < trailing_stop:
            if verbose:
                print(f"🔻 Stop alcanzado en {price:.2f} para {symbol}")
            return (price - entry_price) / entry_price * 100

    return (hist['Close'].iloc[-1] - entry_price) / entry_price * 100

def backtest(start_date: datetime = START_DATE, end_date: datetime = END_DATE, verbose: bool = False):
    """Ejecuta un backtest simple entre dos fechas."""

    capital = INITIAL_BALANCE
    results = []
    error_429_count = 0
    MAX_429 = 10

    print(f"\n🧪 Iniciando backtest entre {start_date.date()} y {end_date.date()}...\n")

    for symbol in stock_assets[:30]:  # Ajusta a más si no hay errores
        try:
            df = pd.DataFrame(fetch_yfinance_stock_data(symbol, verbose=True)).T
            df.columns = ["market_cap", "volume", "weekly_change", "trend", "price_change_24h", "volume_7d_avg"]
            df = df.dropna()

            if df.empty:
                continue

            row = df.iloc[0]
            score = 0
            if row.market_cap > 500_000_000:
                score += 2
            if row.volume > 5_000_000:
                score += 2
            if row.weekly_change > 4:
                score += 1
            if row.trend:
                score += 1
            if row.price_change_24h < 10:
                score += 1
            if row.volume > row.volume_7d_avg:
                score += 1

            if verbose:
                print(
                    f"📊 {symbol} | MC: {row.market_cap}, V: {row.volume}, \u03947d: {row.weekly_change}, "
                    f"Trend: {row.trend}, \u039424h: {row.price_change_24h}, V_avg: {row.volume_7d_avg}"
                )

            if score >= MIN_CRITERIA and is_approved_by_finnhub_and_alphavantage(symbol):
                hist_url = (
                    f"https://query1.finance.yahoo.com/v7/finance/download/{symbol}?period1="
                    f"{(start_date - timedelta(days=1)).timestamp():.0f}&period2="
                    f"{(end_date + timedelta(days=1)).timestamp():.0f}&interval=1d&events=history"
                )
                hist = pd.read_csv(hist_url, parse_dates=["Date"])
                hist = hist.set_index("Date").loc[start_date:end_date]

                pct_return = simulate_trade(symbol, hist, verbose=verbose)
                if pct_return is not None:
                    results.append(pct_return)
                    capital *= (1 + pct_return / 100)

        except Exception as e:
            print(f"⚠️ Error procesando {symbol}: {e}")
            if "429" in str(e):
                error_429_count += 1
                if error_429_count >= MAX_429:
                    print("⛔ Demasiados errores 429. Terminando backtest anticipadamente.")
                    break

        time.sleep(1.5)  # Previene rate limiting

    print("\n✅ Backtest finalizado.")
    print(f"Capital inicial: {INITIAL_BALANCE:.2f} USD")
    print(f"Capital final:   {capital:.2f} USD")
    print(f"Total operaciones simuladas: {len(results)}")
    if results:
        print(f"Rentabilidad promedio por operación: {sum(results)/len(results):.2f}%")
        print(f"Operaciones positivas: {sum(r > 0 for r in results)}")
        print(f"Operaciones negativas: {sum(r < 0 for r in results)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run stock backtesting over a date range")
    parser.add_argument("--start-date", type=str, help="Fecha inicial (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, help="Fecha final (YYYY-MM-DD)")
    parser.add_argument("--verbose", action="store_true", help="Imprime información detallada")
    args = parser.parse_args()

    start = START_DATE if not args.start_date else datetime.strptime(args.start_date, "%Y-%m-%d")
    end = END_DATE if not args.end_date else datetime.strptime(args.end_date, "%Y-%m-%d")

    backtest(start_date=start, end_date=end, verbose=args.verbose)
