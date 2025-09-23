#alpaca.py

import os
import alpaca_trade_api as tradeapi
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
from utils.logger import log_event
from data.providers import get_price


load_dotenv()
api = tradeapi.REST(
    os.getenv("APCA_API_KEY_ID"),
    os.getenv("APCA_API_SECRET_KEY"),
    "https://paper-api.alpaca.markets",
    api_version="v2",
)

# Configure basic retry logic on the underlying HTTP session
retry = Retry(total=3, backoff_factor=3)
adapter = HTTPAdapter(max_retries=retry)
api._session.mount("https://", adapter)
api._session.mount("http://", adapter)

def supports_bracket_trailing() -> bool:
    """Return whether Alpaca allows trailing stops inside bracket orders."""
    # Alpaca currently does not allow trailing stops within standard brackets.
    return False


def supports_fractional_shares() -> bool:
    """Indicate if Alpaca supports fractional share trading."""
    return True


def is_market_open(ttl: int = 60):
    """Compatibility wrapper around :func:`core.market_gate.is_us_equity_market_open`."""

    try:
        from core.market_gate import is_us_equity_market_open

        return is_us_equity_market_open()
    except Exception as exc:  # pragma: no cover - defensive fallback
        log_event(f"❌ Error checking market gate: {exc}")
        return False


def get_current_price(symbol, ttl: int = 30):
    """Return the latest trade price using the shared provider cascade."""

    price, _, _, _, _ = get_price(symbol)
    return float(price) if price is not None else None


# ---------------------------------------------------------------------------
# Additional helpers for robust order handling
# ---------------------------------------------------------------------------

def order_exists(client_order_id: str) -> bool:
    """Return True if an order with ``client_order_id`` exists."""
    try:  # pragma: no cover - network call
        api.get_order_by_client_order_id(client_order_id)
        return True
    except Exception:
        return False


def submit_order(
    *,
    symbol: str,
    side: str,
    qty: float,
    client_order_id: str,
    order_type: str,
    price_ctx: dict | None = None,
):
    """Submit an order and return ``(ok, broker_order_id)``."""
    price_ctx = price_ctx or {}
    try:  # pragma: no cover - network call
        order = api.submit_order(
            symbol=symbol,
            side=side,
            qty=qty,
            type=order_type,
            client_order_id=client_order_id,
            **price_ctx,
        )
        return True, getattr(order, "id", None)
    except Exception as e:  # pragma: no cover
        log_event(f"❌ submit_order failed: {e}")
        return False, None


def get_order_status_by_client_id(client_order_id: str):
    """Return a simple object with order status information."""
    try:  # pragma: no cover - network call
        o = api.get_order_by_client_order_id(client_order_id)
        return type(
            "Status",
            (),
            {
                "state": getattr(o, "status", None),
                "filled_qty": float(getattr(o, "filled_qty", 0) or 0),
                "filled_avg_price": float(getattr(o, "filled_avg_price", 0) or 0),
            },
        )
    except Exception:
        return None


def list_open_orders_today():
    """Return today's open orders."""
    try:  # pragma: no cover - network call
        return api.list_orders(status="open", limit=500)
    except Exception:
        return []


def list_positions():
    """Return current open positions."""
    try:  # pragma: no cover - network call
        return api.list_positions()
    except Exception:
        return []

