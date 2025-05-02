from datetime import datetime
from broker.alpaca import api, get_current_price
from signals.filters import is_position_open
from utils.logger import log_event
# Control open positions and daily logs
open_positions = set()
pending_opportunities = set()
pending_trades = set()
executed_symbols_today = set()
DAILY_INVESTMENT_LIMIT_PCT = 0.30
invested_today_usd = 0
last_investment_day = datetime.utcnow().date()

def reset_daily_investment():
    global invested_today_usd, last_investment_day, executed_symbols_today
    today = datetime.utcnow().date()
    if today != last_investment_day:
        invested_today_usd = 0
        last_investment_day = today
        executed_symbols_today.clear()

def place_order_with_trailing_stop(symbol, amount_usd, trail_percent=2.0):
    reset_daily_investment()
    global invested_today_usd

    try:
        account = api.get_account()
        equity = float(account.equity)

        if invested_today_usd + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT:
            print("⛔ Límite de inversión alcanzado para hoy.")
            return

        if symbol in open_positions or symbol in executed_symbols_today:
            print(f"⚠️ {symbol} ya ejecutado o con posición abierta.")
            return

        if is_position_open(symbol):
            print(f"⚠️ Ya hay una posición abierta en {symbol}. No se realiza nueva compra.")
            return

        current_price = get_current_price(symbol)
        if not current_price:
            print(f"❌ Precio no disponible para {symbol}")
            return

        qty = int(amount_usd // current_price)
        if qty == 0:
            print(f"⚠️ Fondos insuficientes para comprar {symbol}")
            return

        # Enviar orden de compra
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='buy',
            type='market',
            time_in_force='gtc'
        )

        print(f"🛒 Orden de compra enviada para {symbol}. Esperando ejecución...")

        # Esperar ejecución
        while True:
            order_status = api.get_order(order.id).status
            if order_status == 'filled':
                break
            time.sleep(1)

        # Calcular trailing stop
        trail_price = round(current_price * (trail_percent / 100), 2)

        # Enviar orden trailing stop
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell',
            type='trailing_stop',
            time_in_force='gtc',
            trail_price=trail_price
        )

        open_positions.add(symbol)
        invested_today_usd += qty * current_price
        executed_symbols_today.add(symbol)
        pending_trades.add(f"{symbol}: {qty} unidades")

        log_event(f"✅ Compra y trailing stop colocados para {symbol}: {qty} unidades")
    except Exception as e:
        log_event(f"❌ Error placing long order for {symbol}: {str(e)}")



def place_short_order_with_trailing_buy(symbol, amount_usd, trail_percent=2.0):
    reset_daily_investment()
    global invested_today_usd

    try:
        account = api.get_account()
        equity = float(account.equity)

        if invested_today_usd + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT:
            print("⛔ Límite de inversión alcanzado para hoy.")
            return

        if symbol in open_positions or symbol in executed_symbols_today:
            print(f"⚠️ {symbol} ya ejecutado o con posición abierta.")
            return

        if is_position_open(symbol):
            print(f"⚠️ Ya hay una posición abierta en {symbol}. No se realiza nuevo short.")
            return

        current_price = get_current_price(symbol)
        if not current_price:
            print(f"❌ Precio no disponible para {symbol}")
            return

        qty = int(amount_usd // current_price)
        if qty == 0:
            print(f"⚠️ Fondos insuficientes para short en {symbol}")
            return

        # Enviar orden de venta en corto
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell',
            type='market',
            time_in_force='gtc'
        )

        print(f"📉 Orden short enviada para {symbol}. Esperando ejecución...")

        # Esperar ejecución
        while True:
            order_status = api.get_order(order.id).status
            if order_status == 'filled':
                break
            time.sleep(1)

        trail_price = round(current_price * (trail_percent / 100), 2)

        # Enviar orden de recompra con trailing stop
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side='buy',
            type='trailing_stop',
            time_in_force='gtc',
            trail_price=trail_price
        )

        open_positions.add(symbol)
        invested_today_usd += qty * current_price
        executed_symbols_today.add(symbol)
        pending_trades.add(f"{symbol} SHORT: {qty} unidades")

        log_event(f"✅ Short y recompra trailing colocadas para {symbol}: {qty} unidades")
    except Exception as e:
        log_event(f"❌ Error placing short order for {symbol}: {str(e)}")

