from broker.alpaca import api, get_current_price
from broker.account import get_account_equity_safe
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
        equity = get_account_equity_safe()
        if equity <= 0:
            log_event('RISK: ❌ equity inválido para opciones. Se omite compra.')
            return
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

def fetch_valid_option_contract(symbol, strike_offset=0):
    try:
        current_price = get_current_price(symbol)
        if not current_price:
            return None

        strike_price = round(current_price * (1 + 0.02 * strike_offset), 2)
        expiration_date = (datetime.utcnow().date()).strftime("%y%m%d")
        return {
            "symbol": symbol,
            "strike": strike_price,
            "expiry": expiration_date,
            "type": "call",
            "contracts": 1
        }
    except Exception as e:
        log_event(f"❌ Error generando contrato simulado: {e}")
        return None

def buy_option_contract(contract):
    buy_simple_call_option(
        symbol=contract["symbol"],
        strike_price=contract["strike"],
        expiration_date=contract["expiry"],
        contracts=contract.get("contracts", 1)
    )

def run_options_strategy():
    for symbol, offset in [("AAPL", 0), ("MSFT", 0)]:
        contract = fetch_valid_option_contract(symbol, strike_offset=offset)
        if contract:
            buy_option_contract(contract)
        else:
            log_event(f"⚠️ No se encontró contrato válido para {symbol}")
