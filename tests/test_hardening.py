"""Tests offline del endurecimiento 2026-09-10 (sin red)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("APCA_KEY", "PKTEST00000000000000")
os.environ.setdefault("APCA_SEC", "x")

import common  # noqa: E402
import kpi_report  # noqa: E402

LIM = common.load_limits()


def test_log_no_puede_relajar_umbral_real():
    plog = {"parametros_activos": {"score_threshold_real_account": 7.0, "score_min_real": 7.5,
                                   "max_cowork_positions": 50}}
    L = common.effective_limits(plog, LIM)
    assert L["real"]["min_score"] >= 9.0
    assert L["paper"]["max_positions"] <= LIM["paper"]["max_positions"]


def test_log_si_puede_endurecer():
    plog = {"parametros_activos": {"score_threshold_real_account": 9.6, "max_cowork_positions": 2}}
    L = common.effective_limits(plog, LIM)
    assert L["real"]["min_score"] == 9.6 and L["paper"]["max_positions"] == 2


def test_real_auto_depende_del_gate():
    import copy
    auto = copy.deepcopy(LIM); auto["real"]["enabled"] = "auto"
    off = common.effective_limits({"kpis": {"rendimiento_verificado": {"gate_real_ok": False}}}, auto)
    on = common.effective_limits({"kpis": {"rendimiento_verificado": {"gate_real_ok": True}}}, auto)
    killed = common.effective_limits({"parametros_activos": {"real_trading_enabled": False},
                                      "kpis": {"rendimiento_verificado": {"gate_real_ok": True}}}, auto)
    assert off["real"]["active"] is False and on["real"]["active"] is True and killed["real"]["active"] is False


def test_real_forzado_on_respeta_kill_switch_del_log():
    import copy
    forced = copy.deepcopy(LIM); forced["real"]["enabled"] = True
    assert common.effective_limits({}, forced)["real"]["active"] is True
    assert common.effective_limits({"parametros_activos": {"real_trading_enabled": False}}, forced)["real"]["active"] is False


def test_fuentes_momentum_y_apalancados_bloqueados_en_real():
    assert "live_open_scan" in LIM["real"]["blocked_sources"]
    assert "dynamic_scanner_gha" in LIM["real"]["blocked_sources"]
    L = common.effective_limits({}, LIM)
    assert {"TQQQ", "SOXL", "UPRO", "FNGU", "TECL"} <= L["leveraged"]
    assert LIM["real"]["allow_leveraged_etf"] is False


def test_niveles_rr_y_acotado():
    ex = LIM["execution"]
    lv = common.compute_levels(100.0, 0.05, 2.0, ex)            # 1.5×ATR = 3% < 5% → 5%
    assert lv["stop"] == 95.0 and lv["tp"] == 110.0
    lv = common.compute_levels(100.0, 0.01, 10.0, ex)           # 1.5×ATR = 15% → tope 8%
    assert lv["stop_pct"] == ex["stop_pct_max"]
    assert abs((lv["tp"] - 100) / (100 - lv["stop"]) - ex["min_rr"]) < 0.01


def test_sizing_por_riesgo_y_topes():
    # 0.5% de 100k = 500$ de riesgo; 5$/acción → 100 acc; tope 10% → 100 acc a 100$
    assert common.size_by_risk(100_000, 100, 5, 0.5, 10, 1e9) == 100
    assert common.size_by_risk(100_000, 100, 1, 0.5, 10, 1e9) == 100   # tope por % posición
    assert common.size_by_risk(100_000, 100, 5, 0.5, 10, 2_000) == 19  # tope buying power
    assert common.size_by_risk(4_548, 100, 5, 0.5, 12, 4_548) == 4     # cuenta real
    assert common.size_by_risk(1_000, 500, 25, 0.5, 12, 1_000) == 0


def test_guardrails_idempotentes():
    pl = {"parametros_activos": {"score_threshold_real_account": 8.5, "score_min_real": 8.5,
                                 "be_lock_R_threshold_ew": 0.3, "breakeven_stop_at_r": 0.5,
                                 "toma_parciales": {"activado": True}, "r_ratio_minimo": 1.0}}
    c1 = common.enforce_guardrails(pl)
    p = pl["parametros_activos"]
    assert p["score_threshold_real_account"] == 9.0 and p["be_lock_R_threshold_ew"] == 1.0
    assert p["toma_parciales"]["activado"] is False and p["r_ratio_minimo"] == 2.0
    assert c1 and common.enforce_guardrails(pl) == []
    # Una tarea vuelve a relajar el umbral real → se re-aplica; la gestión ya no se toca
    p["score_min_real"] = 8.0; p["be_lock_R_threshold_ew"] = 0.5
    assert common.enforce_guardrails(pl) == ["score_min_real: 8.0 → 9.0"]


def test_fifo_trades():
    f = [{"symbol": "AAA", "qty": "10", "price": "10", "side": "buy", "transaction_time": "2026-09-01T14:00:00Z"},
         {"symbol": "AAA", "qty": "5", "price": "12", "side": "sell", "transaction_time": "2026-09-02T14:00:00Z"},
         {"symbol": "AAA", "qty": "5", "price": "9", "side": "sell", "transaction_time": "2026-09-03T14:00:00Z"},
         {"symbol": "BTC/USD", "qty": "1", "price": "100", "side": "buy", "transaction_time": "2026-09-01T14:00:00Z"},
         {"symbol": "BTC/USD", "qty": "1", "price": "90", "side": "sell", "transaction_time": "2026-09-02T14:00:00Z"}]
    tr = kpi_report.closed_trades(f)
    eq = [t for t in tr if not t[3]]; cr = [t for t in tr if t[3]]
    assert [round(t[2], 2) for t in eq] == [10.0, -5.0] and round(cr[0][2], 2) == -10.0
    s = kpi_report.stats(eq)
    assert s["n"] == 2 and s["profit_factor"] == 2.0 and s["expectancy_usd"] == 2.5
