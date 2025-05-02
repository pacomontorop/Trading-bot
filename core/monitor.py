import time
from broker.alpaca import api
from utils.logger import log_event

def monitor_open_positions():
    print("🟢 Monitor de posiciones iniciado.")
    while True:
        try:
            positions = api.list_positions()
            if not positions:
                print("⚠️ No hay posiciones abiertas actualmente.")
                time.sleep(3600)
                continue

            positions_data = []
            for p in positions:
                symbol = p.symbol
                qty = float(p.qty)
                avg_entry_price = float(p.avg_entry_price)
                current_price = float(p.current_price)
                change_percent = (current_price - avg_entry_price) / avg_entry_price * 100
                positions_data.append((symbol, qty, avg_entry_price, current_price, change_percent))

            top_positions = sorted(positions_data, key=lambda x: abs(x[4]), reverse=True)[:5]

            print("📈 Top 5 cambios relativos de posiciones abiertas:")
            for symbol, qty, avg_entry_price, current_price, change_percent in top_positions:
                print(f"🔹 {symbol}: {qty} unidades")
                print(f"   Entrada: {avg_entry_price} | Actual: {current_price}")
                print(f"   Cambio: {change_percent:.2f}%")
                print("-" * 40)

            log_event("✅ Monitorización de posiciones completada correctamente.")

        except Exception as e:
            print(f"❌ Error monitorizando posiciones: {e}")
            log_event(f"❌ Error monitorizando posiciones: {e}")

        time.sleep(3600)
