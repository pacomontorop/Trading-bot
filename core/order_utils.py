from datetime import date
from broker.alpaca import api


def make_client_order_id(symbol: str, side: str, strategy_ver: str) -> str:
    return f"{side.upper()}-LONG-{symbol}-{date.today():%Y%m%d}-{strategy_ver}"


def alpaca_order_exists(client_order_id: str) -> bool:
    try:
        api.get_order_by_client_order_id(client_order_id)
        return True
    except Exception:
        return False
