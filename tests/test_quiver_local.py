import os
from dotenv import load_dotenv
from signals.quiver_approval import is_approved_by_quiver, get_all_quiver_signals

load_dotenv()  # ← Carga las variables de entorno desde el .env

test_symbols = ["AAPL", "MSFT", "NVDA", "FAKE123"]  # Incluye uno falso para ver qué pasa

for symbol in test_symbols:
    print(f"\n🔍 {symbol}")
    approved = is_approved_by_quiver(symbol)
    print(f"✅ Aprobado por Quiver: {approved}")

    signals = get_all_quiver_signals(symbol)
    print(f"📊 Señales activas: {[k for k, v in signals.items() if v]}")
