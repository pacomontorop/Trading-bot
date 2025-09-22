from decimal import Decimal

from libs.broker.ticks import round_stop_price


def test_round_stop_equity_ge_1():
    assert round_stop_price("XYZ", "SELL", 4.6000000000000005) == Decimal("4.60")
    assert round_stop_price("XYZ", "BUY", 4.6000000000000005) == Decimal("4.60")


def test_round_stop_subdollar():
    assert round_stop_price("PENNY", "SELL", 0.123456) == Decimal("0.1234")
    assert round_stop_price("PENNY", "BUY", 0.123456) == Decimal("0.1235")
