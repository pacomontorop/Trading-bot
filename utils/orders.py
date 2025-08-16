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
