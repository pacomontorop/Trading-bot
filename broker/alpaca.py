#alpaca.py

import os

import alpaca_trade_api as tradeapi
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv

from data.providers import get_price
from utils.logger import log_event
from utils.system_log import get_logger


load_dotenv()
log = get_logger("broker.alpaca")


class _LazyREST:
    """Lazy-initialised Alpaca REST client proxy.

    The real ``tradeapi.REST`` instance is created on first attribute access,
    so the module can be imported without valid API credentials (e.g. in
    tests).  Set ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY`` in the
    environment (or ``.env``) before any actual broker call is made.
    """

    _client: "tradeapi.REST | None" = None

    def _init_client(self) -> "tradeapi.REST":
        if self._client is not None:
            return self._client

        base_url = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
        client = tradeapi.REST(
            os.getenv("APCA_API_KEY_ID"),
            os.getenv("APCA_API_SECRET_KEY"),
            base_url,
            api_version="v2",
        )

        # Configure retry logic on the underlying HTTP session
        retry = Retry(total=3, backoff_factor=3)
        adapter = HTTPAdapter(max_retries=retry)
        client._session.mount("https://", adapter)
        client._session.mount("http://", adapter)

        try:  # pragma: no cover - network dependent
            acct = client.get_account()
            log.info(
                f"[ALPACA] connected base_url={base_url} "
                f"account_id={getattr(acct, 'id', 'unknown')} "
                f"buying_power={getattr(acct, 'buying_power', '0')}"
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            log.warning(f"[ALPACA_ENV] unable to fetch account details: {exc}")

        self._client = client
        return client

    def __getattr__(self, name: str):  # noqa: ANN001
        return getattr(self._init_client(), name)


api = _LazyREST()

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


def get_todays_filled_buy_orders(ny_date: str) -> list | None:
    """Return today's filled buy orders from Alpaca.

    Queries Alpaca for all orders since midnight NY time on ``ny_date`` and
    returns only filled buy-side orders.  Returns ``None`` if the API call
    fails so callers can distinguish "no orders" from "call failed".

    Args:
        ny_date: Date string ``'YYYY-MM-DD'`` in New York timezone.
    """
    try:  # pragma: no cover - network call
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ny_tz = ZoneInfo("America/New_York")
        midnight = datetime.strptime(ny_date, "%Y-%m-%d").replace(
            hour=0, minute=0, second=0, tzinfo=ny_tz
        )
        orders = api.list_orders(
            status="all",
            after=midnight.isoformat(),
            direction="asc",
            limit=500,
        )
        return [
            o
            for o in (orders or [])
            if getattr(o, "side", "") == "buy"
            and getattr(o, "status", "") == "filled"
        ]
    except Exception:
        return None

