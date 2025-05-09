from broker.alpaca import api
from utils.logger import log_event
from datetime import datetime

OPTIONS_INVESTMENT_LIMIT_PCT = 0.10
_last_option_day = datetime.utcnow().date()
_total_options_invested_today = 0.0
options_executed_log = []

def reset_option_investment():
    global _total_options_invested_today, _last_option_day
    today = datetime.utcnow().date()
    if today != _last_option_day:
        _total_options_invested_today = 0.0
        _last_option_day = today

def add_to_options_invested(amount):
    global _total_options_invested_today
    _total_options_invested_today += amount

def options_invested_today_usd():
    return _total_options_invested_today

def get_options_log_and_reset():
    global options_executed_log
    logs = options_executed_log.copy()
    options_executed_log = []
    return logs

def buy_simple_call_option(symbol, strike_price, expiration_date, contracts=1):
    reset_option_investment()
    try:
        account = api.get_account()
        equity = float(account.equity)
        max_allowed = equity * OPTIONS_INVESTMENT_LIMIT_PCT

        estimated_cost = strike_price * 100 * contracts
        if options_invested_today_usd() + estimated_cost > max_allowed:
            print("⛔ Límite de inversión en opciones alcanzado para hoy.")
            return

        option_symbol = f"{symbol}{expiration_date}C{int(strike_price * 1000):08d}"

        order = api.submit_order(
            symbol=option_symbol,
            qty=contracts,
            side='buy',
            type='market',
            time_in_force='day'
        )

        print(f"✅ Orden de CALL enviada para {option_symbol}")
        add_to_options_invested(estimated_cost)
        log_event(f"📘 Opción comprada: {option_symbol}, contratos={contracts}, estimado={estimated_cost}")
        options_executed_log.append(f"{option_symbol} ({contracts} contratos) ~{estimated_cost} USD")

    except Exception as e:
        print(f"❌ Error comprando opción: {e}")
        log_event(f"❌ Error comprando opción {symbol}: {e}")

def run_options_strategy():
    # Aquí defines una lógica básica de ejemplo (puedes cambiar los símbolos)
    from datetime import datetime, timedelta
    today = datetime.utcnow()
    next_friday = today + timedelta((4 - today.weekday()) % 7)  # el viernes más próximo
    expiration_date = next_friday.strftime("%y%m%d")  # formato YYMMDD

    for symbol, strike in [("AAPL", 150), ("MSFT", 330)]:
        buy_simple_call_option(symbol, strike_price=strike, expiration_date=expiration_date)
