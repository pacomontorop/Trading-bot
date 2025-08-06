from broker.alpaca import get_current_price
from utils.technicals import get_rsi, get_moving_average, is_extremely_volatile


def apply_adaptive_bonus(symbol: str, mode: str = "long") -> int:
    """Apply soft bonus/penalty to final score based on context.

    Parameters
    ----------
    symbol: str
        Ticker symbol being evaluated.
    mode: str, optional
        "long" or "short" to adjust RSI penalty direction.

    Returns
    -------
    int
        Bonus (positive) or penalty (negative). Safe-fails to 0 on errors.
    """
    try:
        bonus = 0

        price = get_current_price(symbol)
        ma7 = get_moving_average(symbol, window=7)
        if price and ma7 and ma7 != 0 and abs(price - ma7) / ma7 <= 0.03:
            bonus += 1

        if is_extremely_volatile(symbol):
            bonus -= 1

        rsi = get_rsi(symbol)
        if rsi is not None:
            if mode == "long" and rsi > 80:
                bonus -= 1
            if mode == "short" and rsi < 20:
                bonus -= 1

        return bonus
    except Exception as e:
        print(f"⚠️ Error en apply_adaptive_bonus para {symbol}: {e}")
        return 0
