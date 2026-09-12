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


# --- intraday-monitor · regla 6: soltar stops ajenos pegados al precio ------------------------

class _Alp:
    """Cliente Alpaca mínimo: registra las llamadas y devuelve OK."""
    name = "paper"

    def __init__(self, pos, ords):
        self._p, self._o, self.calls = pos, ords, []

    def positions(self):
        return self._p

    def open_orders(self):
        return self._o

    def req(self, path, method="GET", data=None):
        self.calls.append((method, path, data))
        return {"id": "x"}


def _pos_dell(price="567.30"):
    return {"symbol": "DELL", "qty": "11", "side": "long", "avg_entry_price": "564.75",
            "current_price": price, "unrealized_plpc": "0.0045", "market_value": "6240",
            "asset_class": "us_equity", "qty_available": "11"}


def _stop(cid, sp="565.87"):
    return {"id": "o1", "symbol": "DELL", "side": "sell", "type": "stop_limit",
            "stop_price": sp, "client_order_id": cid, "status": "new"}


PLOG_DELL = {"operaciones": [{"simbolo": "DELL", "estado": "abierta", "precio_stop": 519.31}]}
M6 = common.load_limits()["management"]


def _run(pos, ords, plog=PLOG_DELL):
    import intraday_monitor as im
    alp = _Alp([pos], ords)
    acc = []
    im.manage(alp, plog, M6, acc)
    return alp, acc


def test_stop_ajeno_pegado_vuelve_al_stop_del_plan():
    alp, acc = _run(_pos_dell(), [_stop("PROTECT.DELL.5658795.760267")])
    patch = [c for c in alp.calls if c[0] == "PATCH"]
    assert len(patch) == 1 and float(patch[0][2]["stop_price"]) == 519.31
    assert "519.31" in acc[0]


def test_no_toca_stops_propios_gha():
    alp, _ = _run(_pos_dell(), [_stop("GHA-PROTECT-202609111600-DELL")])
    assert not [c for c in alp.calls if c[0] == "PATCH"]


def test_no_toca_stop_de_riesgo_por_debajo_de_la_entrada():
    alp, _ = _run(_pos_dell(), [_stop("PROTECT.DELL.1", sp="540.00")])
    assert not [c for c in alp.calls if c[0] == "PATCH"]


def test_sin_stop_planificado_no_hace_nada():
    alp, _ = _run(_pos_dell(), [_stop("PROTECT.DELL.1")], plog={"operaciones": []})
    assert not [c for c in alp.calls if c[0] == "PATCH"]


def test_posicion_ya_en_beneficio_mantiene_el_candado():
    # +1,2R: el candado es legítimo (lo habría puesto la regla 3/4), no se suelta.
    pos = _pos_dell(price="620.00")
    alp, _ = _run(pos, [_stop("PROTECT.DELL.1", sp="600.00")])
    assert not [c for c in alp.calls if c[0] == "PATCH" and float(c[2]["stop_price"]) < 600]


def test_ratchet_deja_correr_a_las_ganadoras():
    # DELL entrada 564.75, R=45.44 (stop del plan 519.31). A +5.3R (precio 805) el stop
    # persigue a 3R: 805 − 136.32 = 668.7. Ni se congela en +1R ni se pega al precio.
    pos = _pos_dell(price="805.00")
    alp, acc = _run(pos, [_stop("GHA-PROTECT-1", sp="610.19")])
    patch = [c for c in alp.calls if c[0] == "PATCH"]
    assert len(patch) == 1
    assert abs(float(patch[0][2]["stop_price"]) - 668.68) < 0.5, patch
    assert "trailing a 3R" in acc[0]


def test_ratchet_no_se_activa_por_debajo_de_4R():
    # +3.3R: todavía no hay ratchet (umbral 4R); el lock de +1R ya está puesto → nada que hacer.
    pos = _pos_dell(price="714.00")
    alp, _ = _run(pos, [_stop("GHA-PROTECT-1", sp="610.19")])
    assert not [c for c in alp.calls if c[0] == "PATCH"]


def test_ratchet_no_baja_el_stop():
    pos = _pos_dell(price="805.00")
    alp, _ = _run(pos, [_stop("GHA-PROTECT-1", sp="700.00")])
    assert not [c for c in alp.calls if c[0] == "PATCH"]


def test_lock_normal_sigue_funcionando_por_debajo_del_ratchet():
    # +2.2R: lock en entrada + 1R = 610.19, todavía sin ratchet.
    pos = _pos_dell(price="665.00")
    alp, acc = _run(pos, [_stop("GHA-PROTECT-1", sp="565.00")])
    patch = [c for c in alp.calls if c[0] == "PATCH"]
    assert len(patch) == 1 and abs(float(patch[0][2]["stop_price"]) - 610.19) < 0.5, patch
    assert "lock" in acc[0]


def test_escala_honesta_del_scanner():
    """La normalización del scanner debe dividir por el máximo ALCANZABLE, no por 12 fijo."""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "dynamic_scanner.py").read_text()
    assert "MAX_ALCANZABLE" in src and "s / MAX_ALCANZABLE * 10" in src
    assert "s / 12 * 10" not in src
    # sin UW_KEY el techo es momentum 9.5 + news 0.8 + sector 0.5 = 10.8 → 10.0 normalizado
    ns = {"MOM_MAX": 9.5, "UW_KEY": ""}
    exec("MAX_NEWS, MAX_UW, MAX_SECTOR = 0.8, 5.0, 0.5\n"
         "MAX_ALCANZABLE = MOM_MAX + MAX_NEWS + MAX_SECTOR + (MAX_UW if UW_KEY else 0.0)", ns)
    assert ns["MAX_ALCANZABLE"] == 10.8
    norm = lambda s: round(min(s / ns["MAX_ALCANZABLE"] * 10, 10.0), 2)
    assert norm(10.8) == 10.0 and norm(9.72) == 9.0
    assert norm(8.30) == 7.69          # DELL del 11-sep: 6.92 con la escala vieja


def test_momentum_por_barras_reproduce_el_scoring():
    """puntua_momentum_barras debe dar el máximo (9.5) en el caso perfecto y 0 sin datos."""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "dynamic_scanner.py").read_text()
    ini = src.index("MOM_MAX = 9.5")
    fin = src.index("# ── FUENTE 2", ini)
    ns = {}
    exec(src[ini:fin], ns)
    f = ns["puntua_momentum_barras"]
    assert f(None) == (0, {}) and f([{"c": 1, "h": 1, "v": 1}] * 5) == (0, {})
    # caso perfecto: tendencia al alza, ruptura del día +6,5 %, volumen x4, en máximos
    cierres = [100.0] * 16 + [102.0, 104.0, 106.0, 108.0, 115.0]
    bs = [{"c": c, "h": c, "v": 4_000_000 if i == 20 else 1_000_000} for i, c in enumerate(cierres)]
    s, st = f(bs)
    assert s == ns["MOM_MAX"], (s, st)          # 2.0+1.5+1.0+2.5+1.0+1.5
    assert st["vol_ratio"] == 4.0 and st["pct_from_21d_high"] == 0.0 and st["ret1d"] == 6.48
    # caída fuerte con volumen bajo → claramente negativo (se descarta en el escaneo)
    bs2 = [{"c": 100.0, "h": 100.0, "v": 1_000_000} for _ in range(20)]
    bs2.append({"c": 90.0, "h": 100.0, "v": 500_000})
    s2, _ = f(bs2)
    assert s2 <= -2.0, s2
