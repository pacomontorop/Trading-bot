from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from core.order_protection import (
    compute_bracket_prices,
    compute_break_even_stop,
    validate_bracket_prices,
)


def _assert_close(actual: float, expected: float, tol: float = 1e-6) -> None:
    assert abs(actual - expected) <= tol, f"{actual} != {expected}"


def test_bracket_payload_rounding() -> None:
    bracket = compute_bracket_prices(symbol="TEST", entry_price=100.0, atr=2.0)
    assert validate_bracket_prices(100.0, bracket["stop_price"], bracket["take_profit"])
    assert bracket["stop_price"] < 100.0
    assert bracket["take_profit"] > 100.0


def test_break_even_move() -> None:
    new_stop = compute_break_even_stop(
        entry_price=100.0,
        initial_stop=95.0,
        last_price=105.0,
        break_even_R=1.0,
        buffer_pct=0.001,
    )
    assert new_stop is not None
    _assert_close(new_stop, 100.1)


if __name__ == "__main__":
    test_bracket_payload_rounding()
    test_break_even_move()
    print("OK")
