def resolve_time_in_force(qty, default="gtc"):
    """Return 'day' if ``qty`` is fractional, otherwise ``default``.

    Args:
        qty: Quantity of shares for the order.
        default: Time in force to use when ``qty`` is a whole number.
    """
    try:
        q = float(qty)
        if abs(q - round(q)) > 1e-6:
            return "day"
    except Exception:
        pass
    return default
