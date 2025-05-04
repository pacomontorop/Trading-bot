import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from signals.reader import stock_assets
from signals.scoring import fetch_yfinance_stock_data

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

def backtest(verbose=False):
    capital = INITIAL_BALANCE
    results = []

    print(f"🧪 Iniciando backtest entre {START_DATE.date()} y {END_DATE.date()}...\n")

    for symbol in stock_assets[:50]:  # Puedes ampliar si quieres
        try:
            # Datos actuales para calcular criterios
            market_cap, volume, weekly_change, trend, price_change_24h, volume_7d_avg = fetch_yfinance_stock_data(symbol)

            score = 0
            if market_cap and market_cap > 500_000_000:
                score += 2
            if volume and volume > 10_000_000:  # menos restrictivo
                score += 2
            if weekly_change and weekly_change > 2:
                score += 1
            if trend:
                score += 1
            if price_change_24h is not None and price_change_24h < 20:
                score += 1
            if volume and volume_7d_avg and volume > volume_7d_avg:
                score += 1

            if score < MIN_CRITERIA:
                continue

            # Descarga histórica de precios
            hist_url = f"https://query1.finance.yahoo.com/v7/finance/download/{symbol}?period1={(START_DATE - timedelta(days=1)).timestamp():.0f}&period2={(END_DATE + timedelta(days=1)).timestamp():.0f}&interval=1d&events=history"
            hist = pd.read_csv(hist_url, parse_dates=["Date"]).set_index("Date").loc[START_DATE:END_DATE]

            pct_return = simulate_trade(symbol, hist, verbose=verbose)
            if pct_return is not None:
                results.append(pct_return)
                capital *= (1 + pct_return / 100)

        except Exception as e:
            print(f"⚠️ Error procesando {symbol}: {e}")

    print("\n✅ Backtest finalizado.")
    print(f"Capital inicial: {INITIAL_BALANCE:.2f} USD")
    print(f"Capital final:   {capital:.2f} USD")
    print(f"Total operaciones simuladas: {len(results)}")

    if results:
        print(f"Rentabilidad promedio por operación: {sum(results)/len(results):.2f}%")
        print(f"Operaciones positivas: {sum(r > 0 for r in results)}")
        print(f"Operaciones negativas: {sum(r < 0 for r in results)}")

        plt.hist(results, bins=30, color="skyblue", edgecolor="black")
        plt.title("Distribución de rendimientos simulados")
        plt.xlabel("Rendimiento (%)")
        plt.ylabel("Frecuencia")
        plt.grid(True)
        plt.show()

if __name__ == "__main__":
    backtest(verbose=True)

