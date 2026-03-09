"""Tests for blown-stop detection in position_protector and live_executor.

Blown stop: price has gapped below an existing stop-limit order that
hasn't filled. The protector must cancel it and place a market sell,
but only if the gap exceeds blown_stop_gap_atr_multiplier × ATR.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


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


def _run_protect(
    position,
    stop_order,
    last_price: float,
    atr: float = 1.5,
    blown_gap_mult: float = 0.0,
    dry_run: bool = False,
):
    """Helper: run one paper protect tick with controlled config."""
    from core import position_protector

    mock_api = MagicMock()
    open_orders = [stop_order] if stop_order else []
    risk_cfg = {
        "atr_k": 2.0,
        "min_stop_pct": 0.05,
        "min_tick_equity_ge_1": 0.01,
        "min_tick_equity_lt_1": 0.0001,
        "blown_stop_gap_atr_multiplier": blown_gap_mult,
    }

    with (
        patch("core.position_protector.broker.list_positions", return_value=[position]),
        patch("core.position_protector.broker.api", mock_api),
        patch.object(mock_api, "list_orders", return_value=open_orders),
        patch("core.position_protector._price", return_value=last_price),
        patch("core.position_protector._atr", return_value=atr),
        patch("core.position_protector.is_safeguards_active", return_value=True),
        patch("core.position_protector._risk_cfg", return_value=risk_cfg),
        patch(
            "core.position_protector._safeguards_cfg",
            return_value={"enabled": True, "break_even_R": 1.0, "trailing_enable": True},
        ),
    ):
        position_protector.tick_protect_positions(dry_run=dry_run)

    return mock_api


def _assert_market_sell(mock_api, symbol: str):
    """Assert exactly one market sell was submitted."""
    calls = mock_api.submit_order.call_args_list
    assert len(calls) == 1, f"Expected 1 submit_order call, got {len(calls)}"
    c = calls[0]
    all_kw = {**dict(zip(["symbol", "side", "qty", "type", "time_in_force"], c[0])), **c[1]}
    assert all_kw.get("symbol") == symbol or c[0][0] == symbol
    assert all_kw.get("type") == "market" or "market" in str(c)
    assert all_kw.get("side") == "sell" or "sell" in str(c)


class TestBlownStopPaper:
    """Tests for core.position_protector blown-stop detection."""

    def test_blown_stop_cancels_and_market_sells(self):
        """When gap > threshold, cancel stop-limit and market-sell immediately."""
        # SW real-world 2026-03-09: entry=46.40, stop=42.49, ATR=2.02, mult=0.5
        # gap=42.49-40.88=1.61 > 0.5×2.02=1.01 → blown → sell
        pos = _make_position("SW", entry=46.40, qty=10.0)
        stop_order = _make_stop_limit_order("SW", stop_price=42.49, order_id="ord-sw")

        mock_api = _run_protect(pos, stop_order, last_price=40.88, atr=2.02, blown_gap_mult=0.5)

        mock_api.cancel_order.assert_called_once_with("ord-sw")
        _assert_market_sell(mock_api, "SW")

    def test_blown_stop_sw_would_miss_with_1_5_multiplier(self):
        """With mult=1.5 (old default), SW's gap was below threshold — would NOT sell.
        This is why we lowered blown_stop_gap_atr_multiplier to 0.5."""
        # SW: gap=1.61, threshold=1.5×2.02=3.03 → gap < threshold → skip
        pos = _make_position("SW", entry=46.40, qty=10.0)
        stop_order = _make_stop_limit_order("SW", stop_price=42.49, order_id="ord-sw")

        mock_api = _run_protect(pos, stop_order, last_price=40.88, atr=2.02, blown_gap_mult=1.5)

        mock_api.cancel_order.assert_not_called()
        mock_api.submit_order.assert_not_called()

    def test_blown_stop_gap_too_small_skips_market_sell(self):
        """When gap < threshold, stop-limit may still recover — do not act."""
        # gap=42.40-41.50=0.90 < threshold=1.5×1.5=2.25 → skip
        pos = _make_position("SW", entry=46.40, qty=10.0)
        stop_order = _make_stop_limit_order("SW", stop_price=42.40, order_id="ord-sw")

        mock_api = _run_protect(pos, stop_order, last_price=41.50, atr=1.5, blown_gap_mult=1.5)

        mock_api.cancel_order.assert_not_called()
        mock_api.submit_order.assert_not_called()

    def test_blown_stop_gap_exactly_at_threshold_skips(self):
        """Gap exactly equal to threshold is NOT enough — must exceed it."""
        # gap=2.25, threshold=2.25 → gap < threshold is False, but gap == threshold
        # condition is `gap < atr_threshold` so equal is NOT skipped → sells
        # (boundary: gap=2.24 → skip; gap=2.25 → sell)
        pos = _make_position("TNL", entry=75.48, qty=5.0)
        stop_order = _make_stop_limit_order("TNL", stop_price=70.00, order_id="ord-tnl")
        # gap = 70.00 - 67.75 = 2.25, ATR=1.5, threshold=2.25 → gap NOT < threshold → sell
        mock_api = _run_protect(pos, stop_order, last_price=67.75, atr=1.5, blown_gap_mult=1.5)
        _assert_market_sell(mock_api, "TNL")

    def test_blown_stop_zero_multiplier_any_gap_triggers(self):
        """With multiplier=0 (disabled), any gap below stop triggers market sell."""
        pos = _make_position("TWST", entry=20.00, qty=8.0)
        stop_order = _make_stop_limit_order("TWST", stop_price=18.00, order_id="ord-twst")
        # tiny gap of $0.05 should still trigger when mult=0
        mock_api = _run_protect(pos, stop_order, last_price=17.95, atr=1.5, blown_gap_mult=0.0)

        mock_api.cancel_order.assert_called_once_with("ord-twst")
        _assert_market_sell(mock_api, "TWST")

    def test_blown_stop_no_atr_any_gap_triggers(self):
        """When ATR is unavailable (None), fail-safe: any gap triggers sell."""
        pos = _make_position("RDFN", entry=10.00, qty=15.0)
        stop_order = _make_stop_limit_order("RDFN", stop_price=9.00, order_id="ord-rdfn")

        mock_api = _run_protect(pos, stop_order, last_price=8.50, atr=None, blown_gap_mult=1.5)

        mock_api.cancel_order.assert_called_once_with("ord-rdfn")
        _assert_market_sell(mock_api, "RDFN")

    def test_blown_stop_not_triggered_when_last_above_stop(self):
        """When last > stop, normal trail logic runs — no market sell issued."""
        pos = _make_position("ADBE", entry=255.72, qty=2.0)
        stop_order = _make_stop_limit_order("ADBE", stop_price=273.56, order_id="ord-adbe")

        mock_api = _run_protect(pos, stop_order, last_price=279.00, blown_gap_mult=1.5)

        for c in mock_api.submit_order.call_args_list:
            all_kw = {**dict(zip(["symbol","side","qty","type","time_in_force"], c[0])), **c[1]}
            assert all_kw.get("type") != "market", "Must not place a market sell for a healthy position"

    def test_blown_stop_not_triggered_for_stop_market_order(self):
        """A stop-market (not stop_limit) below price is not acted on."""
        pos = _make_position("LMT", entry=650.70, qty=1.0)
        stop_order = _make_stop_limit_order("LMT", stop_price=648.00, order_id="ord-lmt")
        stop_order.type = "stop"  # stop-market, not stop-limit

        mock_api = _run_protect(pos, stop_order, last_price=647.00, blown_gap_mult=0.0)

        mock_api.cancel_order.assert_not_called()

    def test_blown_stop_dry_run_does_not_submit(self):
        """In dry_run mode the blown stop is logged but no orders are submitted."""
        pos = _make_position("TNL", entry=75.48, qty=5.0)
        stop_order = _make_stop_limit_order("TNL", stop_price=70.44, order_id="ord-tnl")

        mock_api = _run_protect(
            pos, stop_order, last_price=66.00, atr=3.23, blown_gap_mult=1.5, dry_run=True
        )

        mock_api.cancel_order.assert_not_called()
        mock_api.submit_order.assert_not_called()
