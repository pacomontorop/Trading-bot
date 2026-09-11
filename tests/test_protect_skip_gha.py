"""PROTECT (bot Render) no debe tocar posiciones abiertas por GitHub Actions (GHA-*) ni Cowork.

Caso real 2026-09-11: QCOM/QRVO/DELL (entradas GHA con bracket) recibieron stops PROTECT
+0,2 % sobre la entrada (min_profit_lock_pct=0.3) y se cerraron en ~30 min con +0,1 %.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _pos(symbol, entry, qty=50.0):
    p = MagicMock()
    p.symbol, p.avg_entry_price, p.qty, p.side, p.asset_class = symbol, entry, qty, "long", "us_equity"
    return p


def _filled(symbol, cid):
    o = MagicMock()
    o.symbol, o.client_order_id, o.status, o.side, o.type = symbol, cid, "filled", "buy", "limit"
    return o


def _tick(position, recent_orders, last):
    from core import position_protector
    api = MagicMock()
    api.list_orders.side_effect = lambda status="open", limit=500, **k: recent_orders if status == "all" else []
    with (
        patch("core.position_protector.broker.list_positions", return_value=[position]),
        patch("core.position_protector.broker.api", api),
        patch("core.position_protector._price", return_value=last),
        patch("core.position_protector._atr", return_value=3.0),
        patch("core.position_protector.is_safeguards_active", return_value=True),
        patch("core.position_protector._risk_cfg", return_value={"atr_k": 2.0, "min_stop_pct": 0.05,
                                                                  "min_tick_equity_ge_1": 0.01,
                                                                  "min_tick_equity_lt_1": 0.0001}),
        patch("core.position_protector._safeguards_cfg",
              return_value={"enabled": True, "break_even_R": 1.0, "trailing_enable": True}),
    ):
        position_protector.tick_protect_positions(dry_run=False)
    return api


def test_posicion_gha_no_se_toca():
    api = _tick(_pos("QCOM", 184.08), [_filled("QCOM", "GHA-20260911-QCOM")], last=185.0)
    assert api.submit_order.call_count == 0


def test_posicion_cowork_no_se_toca():
    api = _tick(_pos("KFY", 85.37), [_filled("KFY", "COWORK-20260901-KFY")], last=86.0)
    assert api.submit_order.call_count == 0


def test_prefijos_excluidos():
    from core import position_protector
    assert "GHA-REAL-20260911-X".startswith(position_protector.EXTERNALLY_MANAGED_PREFIXES)
    assert not "PROTECT.X.1".startswith(position_protector.EXTERNALLY_MANAGED_PREFIXES)
