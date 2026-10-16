"""Pruebas del parte de proteccion. Todas las funciones probadas son puras: no tocan Alpaca."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from protection_report import (  # noqa: E402
    clasifica_cid,
    cierres_del_dia,
    cobertura_stops,
    marca_caducados,
    motivo_cierre,
    norm,
    resumen_cuenta,
)


def pos(sym, qty=10, entrada=100.0, actual=95.0, pl=-50.0, plpc=-0.05):
    return {"symbol": sym, "qty": str(qty), "avg_entry_price": str(entrada),
            "current_price": str(actual), "market_value": str(qty * actual),
            "unrealized_pl": str(pl), "unrealized_plpc": str(plpc)}


def orden(sym, side="sell", tipo="stop", qty=10, stop=90.0, cid="GHA-PROTECT-x", estado="held"):
    return {"symbol": sym, "side": side, "type": tipo, "qty": str(qty),
            "stop_price": str(stop), "client_order_id": cid, "status": estado,
            "id": f"{sym}-{cid}-{tipo}"}


# ------------------------------------------------------------------ norm y clasificacion

def test_norm_iguala_las_dos_formas_de_alpaca():
    # La trampa de 14-sep: la posicion es SOLUSD y la orden SOL/USD.
    assert norm("SOL/USD") == norm("SOLUSD") == "SOLUSD"


def test_clasifica_cid():
    assert clasifica_cid("GHA-PROTECT-123") == "gha_proteccion"
    assert clasifica_cid("GHA-entry-AAPL") == "gha"
    assert clasifica_cid("PROTECT.AAPL.1") == "heredada_antigua"
    assert clasifica_cid("COWORK-2026-10-13") == "cowork_INESPERADO"
    assert clasifica_cid("f47ac10b-58cc-4372-a567-0e02b2c3d479") == "pata_bracket"
    assert clasifica_cid("") == "sin_id"


def test_heredada_antigua_no_es_lo_mismo_que_cowork():
    # PROTECT.* es legitima y GHA la sigue ratcheteando. COWORK-* reciente no lo es.
    assert clasifica_cid("PROTECT.X") != clasifica_cid("COWORK-X")


def test_motivo_cierre():
    assert motivo_cierre({"type": "stop"}) == "stop"
    assert motivo_cierre({"type": "trailing_stop"}) == "stop"
    assert motivo_cierre({"type": "limit"}) == "objetivo"
    assert motivo_cierre({"type": "market", "client_order_id": "GHA-PROTECT-1"}) == \
        "cierre_forzado_proteccion"
    assert motivo_cierre({"type": "market", "client_order_id": "GHA-x"}) == "cierre_a_mercado"


# ----------------------------------------------------------------------- cobertura stops

def test_posicion_con_stop_held_cuenta_como_protegida():
    # El bug original: la pata en `held` no salia en status=open y se daba por descubierta.
    filas = cobertura_stops([pos("AAPL")], [orden("AAPL", estado="held")])
    assert filas[0]["estado"] == "ok"
    assert filas[0]["stop_px"] == 90.0


def test_cripto_con_simbolos_distintos_cuenta_como_protegida():
    filas = cobertura_stops([pos("SOLUSD", qty=5)], [orden("SOL/USD", qty=5)])
    assert filas[0]["estado"] == "ok"


def test_posicion_sin_stop_se_marca():
    filas = cobertura_stops([pos("NVDA")], [])
    assert filas[0]["estado"] == "SIN_STOP"


def test_orden_limite_no_cuenta_como_stop():
    # Un take-profit no protege de una caida.
    filas = cobertura_stops([pos("MSFT")], [orden("MSFT", tipo="limit")])
    assert filas[0]["estado"] == "SIN_STOP"
    assert filas[0]["n_objetivos"] == 1


def test_stop_que_cubre_menos_acciones_es_parcial():
    filas = cobertura_stops([pos("TSLA", qty=10)], [orden("TSLA", qty=4)])
    assert filas[0]["estado"] == "STOP_PARCIAL"
    assert filas[0]["qty_cubierta"] == 4


def test_stop_del_lado_equivocado_no_protege():
    # Un `buy stop` no cierra una posicion larga.
    filas = cobertura_stops([pos("AMD", qty=10)], [orden("AMD", side="buy")])
    assert filas[0]["estado"] == "SIN_STOP"


def test_posicion_corta_se_protege_con_buy_stop():
    filas = cobertura_stops([pos("SQQQ", qty=-10)], [orden("SQQQ", side="buy", qty=10)])
    assert filas[0]["lado"] == "corto"
    assert filas[0]["estado"] == "ok"


def test_las_desprotegidas_salen_primero():
    filas = cobertura_stops(
        [pos("A"), pos("B"), pos("C")],
        [orden("A"), orden("C")],
    )
    assert filas[0]["simbolo"] == "B"


# ------------------------------------------------------------------------------- cierres

def test_cierres_filtra_por_fecha_y_estado():
    ords = [
        {"symbol": "X", "side": "sell", "type": "stop", "status": "filled",
         "filled_qty": "10", "filled_avg_price": "90", "client_order_id": "GHA-PROTECT-1",
         "filled_at": "2026-10-15T18:00:00Z"},
        {"symbol": "Y", "side": "sell", "type": "stop", "status": "canceled",
         "filled_qty": "0", "filled_avg_price": "0", "client_order_id": "GHA-2",
         "filled_at": ""},
        {"symbol": "Z", "side": "sell", "type": "limit", "status": "filled",
         "filled_qty": "5", "filled_avg_price": "10", "client_order_id": "GHA-3",
         "filled_at": "2026-10-10T18:00:00Z"},
    ]
    out = cierres_del_dia(ords, "2026-10-15T00:00:00Z")
    assert [c["simbolo"] for c in out] == ["X"]
    assert out[0]["motivo"] == "stop"
    assert out[0]["importe_usd"] == 900.0


def test_un_cierre_sin_rastro_queda_identificado():
    # El caso del 13-oct: -575,84 $ y ninguna linea que dijera quien.
    ords = [{"symbol": "W", "side": "sell", "type": "market", "status": "filled",
             "filled_qty": "3", "filled_avg_price": "20", "client_order_id": "",
             "filled_at": "2026-10-15T19:00:00Z"}]
    out = cierres_del_dia(ords, "2026-10-15T00:00:00Z")
    assert out[0]["origen"] == "sin_id"
    assert out[0]["motivo"] == "cierre_a_mercado"


# ------------------------------------------------------------------------------- resumen

def test_resumen_suma_lo_que_sigue_en_riesgo():
    filas = cobertura_stops([pos("A", pl=-100.0), pos("B", pl=40.0)], [orden("A")])
    r = resumen_cuenta("PAPER", {"equity": "99975.35", "last_equity": "100100.00"}, filas, [])
    assert r["pnl_latente_usd"] == -60.0
    assert r["movimiento_dia_usd"] == -124.65
    assert r["n_sin_proteger"] == 1 and r["sin_proteger"] == ["B"]


# --------------------------------------------------------------- sellado de lo caducado

def test_marca_caducados_sella_sin_borrar():
    pl = {"kpis_actuales": {"profit_factor_total": 8.32},
          "kpis": {"rendimiento_global": {"pnl_total_paper_pct": 0.6},
                   "riesgo": {"max_drawdown_paper_pct": 3.45},
                   "rendimiento_verificado": {"paper": {"retorno_pct": -8.3}}}}
    n = marca_caducados(pl)
    assert n == 3
    assert pl["kpis_actuales"]["profit_factor_total"] == 8.32      # no se borra nada
    assert "rendimiento_verificado" in pl["kpis_actuales"]["_OBSOLETO"]
    assert "_OBSOLETO" not in pl["kpis"]["rendimiento_verificado"]  # la buena no se toca


def test_marca_caducados_es_idempotente():
    pl = {"kpis_actuales": {"x": 1}, "kpis": {"rendimiento_global": {}, "riesgo": {}}}
    assert marca_caducados(pl) == 3
    assert marca_caducados(pl) == 0


def test_marca_caducados_aguanta_un_log_sin_esos_bloques():
    pl = {"otra_cosa": 1}
    assert marca_caducados(pl) == 0
