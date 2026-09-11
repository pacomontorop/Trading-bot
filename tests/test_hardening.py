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
    assert L["real"]["min_score"] == 9.6
    # paper es la cuenta de aprendizaje: con ignore_log_limits manda risk_limits.json
    assert L["paper"]["max_positions"] == LIM["paper"]["max_positions"]
    import copy
    strict = copy.deepcopy(LIM); strict["paper"]["ignore_log_limits"] = False
    assert common.effective_limits(plog, strict)["paper"]["max_positions"] == 2


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


def test_ew_scoring_rangos():
    import ew_pipeline
    top = {"asym_pct": 20, "beat_rate": 100, "n_q": 8, "reaction_pct": 5, "rev_up": 5, "rev_down": 0, "mom5": 0, "news": 5}
    sc, parts = ew_pipeline.score_candidate(top, {"vix": 15, "spy_above_ma50": True})
    assert sc == 10.0 and parts["beat_rate"] == 3.0
    flojo = {"asym_pct": -5, "beat_rate": 40, "n_q": 8, "reaction_pct": -3, "rev_up": 0, "rev_down": 3, "mom5": 12}
    sc2, _ = ew_pipeline.score_candidate(flojo, {"vix": 30, "spy_above_ma50": False})
    assert sc2 == 0.0
    # sin datos de Yahoo: la puntuación no puede llegar a real (≥9)
    solo_ew = {"asym_pct": 15, "mom5": 0, "news": 4}
    assert ew_pipeline.score_candidate(solo_ew, {"vix": 15, "spy_above_ma50": True})[0] < 9


def test_pre_close_reglas():
    from datetime import date
    import pre_close
    hoy, nxt = date(2026, 9, 11), date(2026, 9, 14)
    assert pre_close.should_close({"nextEPSDate": "2026-09-11T00:00:00", "releaseTime": 3}, hoy, nxt)[0]
    assert pre_close.should_close({"nextEPSDate": "2026-09-14T00:00:00", "releaseTime": 1}, hoy, nxt)[0]
    assert not pre_close.should_close({"nextEPSDate": "2026-09-14T00:00:00", "releaseTime": 3}, hoy, nxt)[0]
    assert not pre_close.should_close({"nextEPSDate": "2026-09-11T00:00:00", "releaseTime": 1}, hoy, nxt)[0]
    assert not pre_close.should_close(None, hoy, nxt)[0]


def test_paper_aprendizaje_no_afecta_a_real():
    L = common.effective_limits({"parametros_activos": {"score_min_paper": 9.5, "score_threshold_real_account": 8.0}}, LIM)
    assert L["paper"]["min_score"] == LIM["paper"]["min_score"] < 8
    assert L["real"]["min_score"] >= 9.0 and L["real"]["max_positions"] <= 2


def test_bloqueo_dia_riesgo_macro():
    from datetime import datetime
    import market_open_execution as mo
    pl = {"parametros_activos": {"bloqueo_entradas_dias_riesgo_macro": True},
          "macro_context": {"dias_riesgo_macro": ["2026-09-11 CPI 08:30 ET (IMPACTO MAXIMO)"]}}
    assert mo.macro_block(pl, datetime(2026, 9, 11, 9, 40, tzinfo=common.ET))
    assert mo.macro_block(pl, datetime(2026, 9, 11, 10, 35, tzinfo=common.ET)) is None
    assert mo.macro_block(pl, datetime(2026, 9, 14, 9, 40, tzinfo=common.ET)) is None
    pl["parametros_activos"]["bloqueo_entradas_dias_riesgo_macro"] = False
    assert mo.macro_block(pl, datetime(2026, 9, 11, 9, 40, tzinfo=common.ET)) is None


def test_aprendizaje_por_fuente():
    ops = [{"id": "GHA-20260911-DELL", "simbolo": "DELL", "fecha_entrada": "2026-09-11", "fuente": "dynamic_scanner_gha",
            "score_entrada": 7.57, "score_base": 6.92, "ajuste_fuentes": 0.65,
            "fuentes_extra": {"analistas": 0.8, "insiders": -0.3}},
           {"id": "GHA-20260911-AAPL", "simbolo": "AAPL", "fecha_entrada": "2026-09-11", "fuente": "live_open_scan",
            "score_entrada": 6.32, "score_base": 5.62, "ajuste_fuentes": 0.7, "fuentes_extra": {"analistas": 0.55}},
           {"id": "COWORK-1", "simbolo": "KFY", "fecha_entrada": "2026-09-01"}]
    tr = [("2026-09-14", "DELL", 120.0, False), ("2026-09-15", "AAPL", -60.0, False),
          ("2026-09-15", "KFY", 10.0, False), ("2026-09-15", "BTC/USD", 5.0, True),
          ("2026-09-10", "DELL", -30.0, False)]            # cierre anterior a la entrada GHA → "otras"
    atr = kpi_report.atribuir(tr, ops)
    assert [a["fuente"] for a in atr] == ["dynamic_scanner_gha", "live_open_scan", "otras", "otras"]
    apr = kpi_report.aprendizaje(atr, 6.0)
    assert apr["n_total"] == 4 and apr["n_con_fuentes_extra"] == 2
    assert apr["por_senal_extra"]["analistas"]["a_favor"]["n"] == 2
    assert apr["por_senal_extra"]["insiders"]["en_contra"]["net_usd"] == 120.0
    assert apr["solo_entraron_por_fuentes_extra"]["n"] == 1 and apr["solo_entraron_por_fuentes_extra"]["net_usd"] == -60.0
    assert kpi_report.aprendizaje([], 6.0)["n_total"] == 0
