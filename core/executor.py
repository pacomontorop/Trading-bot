# executor.py

from datetime import datetime
import time
from broker.alpaca import api, get_current_price
from signals.filters import is_position_open, is_symbol_approved
from utils.logger import log_event

# Control de estado
open_positions = set()
pending_opportunities = set()
pending_trades = set()
executed_symbols_today = set()
DAILY_INVESTMENT_LIMIT_PCT = 0.50
_last_investment_day = datetime.utcnow().date()
_total_invested_today = 0.0

quiver_signals_log = {}

def reset_daily_investment():
    global _total_investment_day, _last_investment_day, executed_symbols_today
    today = datetime.utcnow().date()
    if today != _last_investment_day:
        _total_invested_today = 0.0
        _last_investment_day = today
        executed_symbols_today.clear()

def add_to_invested(amount):
    global _total_invested_today
    _total_invested_today += amount

def invested_today_usd():
    return _total_invested_today

def wait_for_order_fill(order_id, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            order = api.get_order(order_id)
            if order.status == 'filled':
                return True
            elif order.status in ['canceled', 'rejected']:
                log_event(f"❌ Orden {order_id} cancelada o rechazada: {order.status}")
                return False
        except Exception as e:
            log_event(f"❌ Error verificando estado de orden {order_id}: {e}")
        time.sleep(1)
    log_event(f"❌ Timeout esperando ejecución de orden {order_id}")
    return False

def place_order_with_trailing_stop(symbol, amount_usd, trail_percent=1.5):
    reset_daily_investment()
    try:
        if not is_symbol_approved(symbol):
            print(f"❌ {symbol} no aprobado para compra según criterios de análisis.")
            return

        from signals.quiver_utils import get_all_quiver_signals, score_quiver_signals, QUIVER_APPROVAL_THRESHOLD
        quiver_signals = get_all_quiver_signals(symbol)
        quiver_score = score_quiver_signals(quiver_signals)
        quiver_signals_log[symbol] = [k for k, v in quiver_signals.items() if v]

        account = api.get_account()
        equity = float(account.equity)

        # Nueva excepción: si Quiver score es muy alto (> 10), ignorar límite
        if invested_today_usd() + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT and quiver_score < 10:
            print("⛔ Límite de inversión alcanzado para hoy y Quiver score < 10.")
            return
        elif invested_today_usd() + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT:
            print(f"⚠️ {symbol} excede límite pero Quiver score = {quiver_score} ➜ Se permite excepcionalmente.")

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

        print(f"🛒 Enviando orden de compra para {symbol} por ${amount_usd} → {qty} unidades a ${current_price:.2f} cada una.")
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='buy',
            type='market',
            time_in_force='gtc'
        )

        if not wait_for_order_fill(order.id):
            return

        trail_price = round(current_price * (trail_percent / 100), 2)
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell',
            type='trailing_stop',
            time_in_force='gtc',
            trail_price=trail_price
        )

        open_positions.add(symbol)
        add_to_invested(qty * current_price)
        executed_symbols_today.add(symbol)
        pending_trades.add(f"{symbol}: {qty} unidades — ${qty * current_price:.2f}")

        log_event(f"✅ Compra y trailing stop colocados para {symbol}: {qty} unidades por {qty * current_price:.2f} USD (Quiver score: {quiver_score})")

    except Exception as e:
        log_event(f"❌ Error placing long order for {symbol}: {str(e)}")


def place_short_order_with_trailing_buy(symbol, amount_usd, trail_percent=1.5):
    reset_daily_investment()
    try:
        if not is_symbol_approved(symbol):
            print(f"❌ {symbol} no aprobado para short según criterios de análisis.")
            return

        from signals.quiver_utils import get_all_quiver_signals
        quiver_signals_log[symbol] = [
            k for k, v in get_all_quiver_signals(symbol).items() if v
        ]

        account = api.get_account()
        equity = float(account.equity)

        if invested_today_usd() + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT:
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

        print(f"📉 Enviando orden SHORT para {symbol} por ${amount_usd} → {qty} unidades a ${current_price:.2f} cada una.")
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell',
            type='market',
            time_in_force='gtc'
        )

        if not wait_for_order_fill(order.id):
            return

        trail_price = round(current_price * (trail_percent / 100), 2)
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side='buy',
            type='trailing_stop',
            time_in_force='gtc',
            trail_price=trail_price
        )

        open_positions.add(symbol)
        add_to_invested(qty * current_price)
        executed_symbols_today.add(symbol)
        pending_trades.add(f"{symbol} SHORT: {qty} unidades — ${qty * current_price:.2f}")

        log_event(f"✅ Short y trailing buy colocados para {symbol}: {qty} unidades por {qty * current_price:.2f} USD")

    except Exception as e:
        log_event(f"❌ Error placing short order for {symbol}: {str(e)}")
