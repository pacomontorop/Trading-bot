def resolve_time_in_force(qty, default: str = "gtc", asset_class: str = "us_equity"):
    """Return a suitable ``time_in_force`` based on ``qty`` and ``asset_class``.

    For fractional **equity** orders, Alpaca requires ``time_in_force='day'``. Crypto
    pairs, however, are always fractionable and do not accept ``day`` orders, so we
    fall back to ``default`` for them.

    Args:
        qty: Quantity of shares/units for the order.
        default: Time in force to use when ``qty`` is a whole number or when the
            asset class does not enforce special handling.
        asset_class: Alpaca asset class (e.g. ``us_equity`` or ``crypto``).
    """
    try:
        q = float(qty)
        if abs(q - round(q)) > 1e-6 and asset_class != "crypto":
            return "day"
    except Exception:
        pass
    return default


def enforce_min_price_increment(price: float) -> float:
    """Round ``price`` to the maximum decimals allowed by Alpaca.

    Alpaca rejects limit or stop prices that include more than two decimals when
    the value is greater or equal to 1 USD, or more than four decimals when it is
    below 1 USD.  This helper rounds the provided value accordingly so that
    callers do not need to worry about manual validation.

    Args:
        price: Raw price to be used in an order.

    Returns:
        ``price`` rounded to the permitted number of decimals.
    """

    if price is None:
        return price
    try:
        p = float(price)
    except Exception:
        return price
    return round(p, 2) if p >= 1 else round(p, 4)


def _submit_order_with_class(**kwargs):
    """Internal helper to call ``api.submit_order``.

    Separated for easier testing/mocking.
    """

    from broker.alpaca import api  # Local import to avoid circular deps

    return api.submit_order(**kwargs)


def submit_bracket_order(
    symbol: str,
    qty,
    side: str,
    take_profit: float,
    stop_loss: float,
    limit_price: float | None = None,
    time_in_force: str = "gtc",
):
    """Submit a bracket order combining entry, take-profit and stop-loss.

    Only the bare minimum parameters are exposed; callers can extend this helper
    or submit orders directly for more advanced scenarios.
    """

    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "market" if limit_price is None else "limit",
        "time_in_force": time_in_force,
        "order_class": "bracket",
        "take_profit": {"limit_price": enforce_min_price_increment(take_profit)},
        "stop_loss": {"stop_price": enforce_min_price_increment(stop_loss)},
    }
    if limit_price is not None:
        payload["limit_price"] = enforce_min_price_increment(limit_price)
    return _submit_order_with_class(**payload)


def submit_oco_order(
    symbol: str,
    qty,
    side: str,
    take_profit: float,
    stop_loss: float,
    stop_limit: float | None = None,
    time_in_force: str = "gtc",
):
    """Submit an OCO order combining take-profit and stop-loss after entry."""

    stop_payload = {"stop_price": enforce_min_price_increment(stop_loss)}
    if stop_limit is not None:
        stop_payload["limit_price"] = enforce_min_price_increment(stop_limit)

    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "limit",
        "time_in_force": time_in_force,
        "order_class": "oco",
        "take_profit": {"limit_price": enforce_min_price_increment(take_profit)},
        "stop_loss": stop_payload,
    }
    return _submit_order_with_class(**payload)


def submit_oto_order(
    symbol: str,
    qty,
    side: str,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    limit_price: float | None = None,
    time_in_force: str = "gtc",
):
    """Submit an OTO order with either a stop-loss or take-profit leg."""

    if stop_loss is None and take_profit is None:
        raise ValueError("Either stop_loss or take_profit must be provided")

    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "market" if limit_price is None else "limit",
        "time_in_force": time_in_force,
        "order_class": "oto",
    }
    if limit_price is not None:
        payload["limit_price"] = enforce_min_price_increment(limit_price)
    if take_profit is not None:
        payload["take_profit"] = {
            "limit_price": enforce_min_price_increment(take_profit)
        }
    if stop_loss is not None:
        payload["stop_loss"] = {
            "stop_price": enforce_min_price_increment(stop_loss)
        }
    return _submit_order_with_class(**payload)

