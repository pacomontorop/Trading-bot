"""Tests for blown-stop detection in position_protector and live_executor.

Blown stop: price has gapped below an existing stop-limit order that
hasn't filled. The protector must cancel it and place a market sell.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch


def _make_position(symbol: str, entry: float, qty: float = 10.0):
    pos = MagicMock()
    pos.symbol = symbol
    pos.avg_entry_price = entry
    pos.qty = qty
    pos.side = "long"
    pos.asset_class = "us_equity"
    return pos


def _make_stop_limit_order(symbol: str, stop_price: float, order_id: str = "ord-1"):
    order = MagicMock()
    order.symbol = symbol
    order.side = "sell"
    order.type = "stop_limit"
    order.stop_price = stop_price
    order.id = order_id
    return order


class TestBlownStopPaper:
    """Tests for core.position_protector blown-stop detection."""

    def _run_protect(self, position, stop_order, last_price: float):
        from core import position_protector

        mock_api = MagicMock()
        open_orders = [stop_order] if stop_order else []

        with (
            patch("core.position_protector.broker.list_positions", return_value=[position]),
            patch("core.position_protector.broker.api", mock_api),
            patch.object(mock_api, "list_orders", return_value=open_orders),
            patch("core.position_protector._price", return_value=last_price),
            patch("core.position_protector._atr", return_value=1.5),
            patch("core.position_protector.is_safeguards_active", return_value=True),
            patch(
                "core.position_protector._safeguards_cfg",
                return_value={"enabled": True, "break_even_R": 1.0, "trailing_enable": True},
            ),
        ):
            position_protector.tick_protect_positions(dry_run=False)

        return mock_api

    def test_blown_stop_cancels_and_market_sells(self):
        """When last < stop-limit stop_price, cancel order and market-sell."""
        pos = _make_position("SW", entry=46.40, qty=10.0)
        stop_order = _make_stop_limit_order("SW", stop_price=42.40, order_id="ord-sw")

        mock_api = self._run_protect(pos, stop_order, last_price=41.09)

        # Must have cancelled the stuck stop-limit
        mock_api.cancel_order.assert_called_once_with("ord-sw")

        # Must have submitted a market sell
        submit_calls = mock_api.submit_order.call_args_list
        assert len(submit_calls) == 1
        kwargs = submit_calls[0].kwargs if submit_calls[0].kwargs else submit_calls[0][1]
        # submit_order may be called with positional or keyword args
        args = submit_calls[0][0]
        all_args = {**dict(zip(["symbol", "side", "qty", "type", "time_in_force"], args)), **kwargs}
        assert all_args.get("symbol") == "SW" or args[0] == "SW"
        assert all_args.get("type") == "market" or "market" in str(submit_calls[0])
        assert all_args.get("side") == "sell" or "sell" in str(submit_calls[0])

    def test_blown_stop_not_triggered_when_last_above_stop(self):
        """When last > stop, normal trail logic runs — no market sell issued."""
        pos = _make_position("ADBE", entry=255.72, qty=2.0)
        stop_order = _make_stop_limit_order("ADBE", stop_price=273.56, order_id="ord-adbe")

        mock_api = self._run_protect(pos, stop_order, last_price=279.00)

        # Normal trailing may cancel+replace the stop — but must NOT submit a market sell.
        for c in mock_api.submit_order.call_args_list:
            submitted = {**dict(zip(["symbol","side","qty","type","time_in_force"], c[0])), **c[1]}
            assert submitted.get("type") != "market", "Must not place a market sell for a healthy position"

    def test_blown_stop_not_triggered_for_stop_market_order(self):
        """A stop (not stop_limit) that is below last is not a blown stop — leave it."""
        pos = _make_position("LMT", entry=650.70, qty=1.0)
        stop_order = _make_stop_limit_order("LMT", stop_price=648.00, order_id="ord-lmt")
        stop_order.type = "stop"  # stop-market order, not stop-limit

        mock_api = self._run_protect(pos, stop_order, last_price=647.00)

        # stop-market blown through: not our job to second-guess, no forced cancel
        mock_api.cancel_order.assert_not_called()

    def test_blown_stop_dry_run_does_not_submit(self):
        """In dry_run mode the blown stop is logged but no orders are submitted."""
        from core import position_protector

        pos = _make_position("TNL", entry=75.48, qty=5.0)
        stop_order = _make_stop_limit_order("TNL", stop_price=70.44, order_id="ord-tnl")

        mock_api = MagicMock()
        open_orders = [stop_order]

        with (
            patch("core.position_protector.broker.list_positions", return_value=[pos]),
            patch("core.position_protector.broker.api", mock_api),
            patch.object(mock_api, "list_orders", return_value=open_orders),
            patch("core.position_protector._price", return_value=69.10),
            patch("core.position_protector._atr", return_value=3.23),
            patch("core.position_protector.is_safeguards_active", return_value=True),
            patch(
                "core.position_protector._safeguards_cfg",
                return_value={"enabled": True, "break_even_R": 1.0, "trailing_enable": True},
            ),
        ):
            position_protector.tick_protect_positions(dry_run=True)

        mock_api.cancel_order.assert_not_called()
        mock_api.submit_order.assert_not_called()
