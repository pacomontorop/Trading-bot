"""Order helper utilities."""


def resolve_time_in_force(qty: float) -> str:
    """Return Alpaca time-in-force for equity orders."""

    if abs(qty - round(qty)) > 1e-6:
        return "day"
    return "gtc"
