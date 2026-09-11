"""Tests offline de scripts/fuentes_extra.py y de su integración en market-open (sin red)."""
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("APCA_KEY", "PKTEST00000000000000")
os.environ.setdefault("APCA_SEC", "x")

import fuentes_extra as fx  # noqa: E402

FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>DOE JOHN</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer>
      <officerTitle>CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-08</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>50.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-09</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


def test_form4_parse_y_puntuacion():
    txs = fx.parse_form4(FORM4)
    assert [t["code"] for t in txs] == ["P", "M"]
    assert txs[0]["usd"] == 505000.0 and "CEO" in txs[0]["cargo"] and "consejero" in txs[0]["cargo"]
    r = fx.resumen_insiders(txs)
    assert r["compras_usd"] == 505000 and r["n_compradores"] == 1 and r["ventas_usd"] == 0
    assert fx.puntua_insiders(r) == 0.6
    # dos compradores distintos → señal fuerte; ventas masivas sin compras → resta; nada → 0
    assert fx.puntua_insiders({**r, "n_compradores": 2}) == 1.0
    assert fx.puntua_insiders({"compras_usd": 0, "ventas_usd": 30e6, "n_compradores": 0, "n_vendedores": 3}) == -0.3
    assert fx.puntua_insiders({}) == 0.0 and fx.puntua_insiders(None) == 0.0


def test_form4_recientes_filtra_por_fecha_y_quita_xsl():
    hoy = date.today()
    sub = {"filings": {"recent": {
        "form": ["4", "8-K", "4", "4"],
        "filingDate": [hoy.isoformat(), hoy.isoformat(), (hoy - timedelta(days=5)).isoformat(),
                       (hoy - timedelta(days=90)).isoformat()],
        "accessionNumber": ["0001-26-000001", "x", "0001-26-000002", "0001-26-000003"],
        "primaryDocument": ["xslF345X05/wk-form4_1.xml", "a.htm", "doc4.xml", "old.xml"]}}}
    out = fx.form4_recientes(sub, hoy - timedelta(days=30))
    assert out == [("0001-26-000001", hoy.isoformat(), "wk-form4_1.xml"),
                   ("0001-26-000002", (hoy - timedelta(days=5)).isoformat(), "doc4.xml")]


def test_analistas():
    hoy = date.today()
    rows = [{"GradeDate": hoy.isoformat(), "Firm": "A", "Action": "up", "ToGrade": "Buy", "priceTargetAction": "Raises"},
            {"GradeDate": hoy.isoformat(), "Firm": "B", "Action": "up", "ToGrade": "Buy", "priceTargetAction": "Raises"},
            {"GradeDate": hoy.isoformat(), "Firm": "C", "Action": "main", "ToGrade": "Hold", "priceTargetAction": "Lowers"},
            {"GradeDate": (hoy - timedelta(days=60)).isoformat(), "Firm": "D", "Action": "down"}]
    a = fx.parse_calificaciones(rows, hoy - timedelta(days=30))
    assert (a["subidas"], a["bajadas"], a["obj_sube"], a["obj_baja"]) == (2, 0, 2, 1)
    assert fx.puntua_analistas(a, None) == 0.8                      # 0,8 + 0,15 → tope 0,8
    ficha = {"currentPrice": 100, "targetMeanPrice": 130, "numberOfAnalystOpinions": 12}
    assert fx.puntua_analistas(a, ficha) == 1.0                     # + objetivo +30 % → tope 1,0
    malo = fx.parse_calificaciones([{"GradeDate": hoy.isoformat(), "Action": "down"}] * 3, hoy - timedelta(days=30))
    assert fx.puntua_analistas(malo, {"currentPrice": 100, "targetMeanPrice": 90, "numberOfAnalystOpinions": 8}) == -1.0
    assert fx.puntua_analistas({}, None) == 0.0


def test_opciones_y_corto():
    calls = [{"strike": 100, "volume": 3000, "openInterest": 1000, "impliedVolatility": 0.4},
             {"strike": 110, "volume": float("nan"), "openInterest": 500, "impliedVolatility": 0.5}]
    puts = [{"strike": 100, "volume": 1000, "openInterest": 1000, "impliedVolatility": 0.44}]
    o = fx.resumen_cadena(calls, puts, 101)
    assert o["call_put"] == 3.0 and o["iv_atm_pct"] == 42.0 and o["vol_oi"] == 1.6
    assert fx.puntua_opciones(o) == 0.7                              # C/P alto + actividad inusual
    assert fx.puntua_opciones({**o, "call_vol": 100, "put_vol": 100}) == 0.0   # poco volumen: no cuenta
    assert fx.puntua_opciones({"call_vol": 300, "put_vol": 900, "call_put": 0.33, "vol_oi": 0.2}) == -0.4
    assert fx.puntua_opciones({}) == 0.0
    assert fx.puntua_corto({"shortPercentOfFloat": 0.30}) == -0.4
    assert fx.puntua_corto({"shortPercentOfFloat": 0.03}) == 0.0 and fx.puntua_corto(None) == 0.0


def test_sector():
    assert fx.etf_de("SMH", {"sector": "Technology"}) is None          # ETF: no aplica
    assert fx.etf_de("QRVO", {"sector": "Technology", "industry": "Semiconductors"}) == "SMH"
    assert fx.etf_de("JPM", {"sector": "Financial Services"}) == "XLF"
    assert fx.etf_de("XYZ", None, sic=6798) == "XLRE" and fx.etf_de("XYZ", None, sic=2834) == "XLV"
    spy = [100.0] * 30
    etf = [100.0] * 9 + [100 + i for i in range(21)]                   # +20 % en 20 sesiones
    fr = fx.fuerza_relativa(etf, spy)
    assert fr["rs20"] == 20.0 and fx.puntua_sector(fr) == 0.4
    assert fx.puntua_sector({"rs5": -1, "rs20": -5}) == -0.4 and fx.puntua_sector({}) == 0.0
    assert fx.fuerza_relativa([1.0] * 5, spy) is None


def test_macro_fred():
    txt = "observation_date,BAMLH0A0HYM2\n2026-08-01,3.00\n2026-08-02,.\n" + \
          "".join(f"2026-08-{d:02d},{3.0 + d * 0.03:.2f}\n" for d in range(3, 26))
    hy = fx.parse_fred_csv(txt)
    assert hy[0] == ("2026-08-01", 3.0) and all(v != "." for _, v in hy)
    m = fx.regimen_macro(hy, [("2026-08-20", -0.4)], [("x", 0.5)], {"score": 50})
    assert m["regimen"] == "risk_off" and m["ajuste"] == -0.5          # diferencial HY +0,6 pp en 20 obs
    calma = [("d", 3.0)] * 25
    assert fx.regimen_macro(calma, [("d", -0.5)], None, None)["ajuste"] == 0.0
    assert fx.regimen_macro(None, None, None, {"score": 10})["ajuste"] == -0.2   # pánico
    peor = fx.regimen_macro(hy, [("d", 1.0)], None, {"score": 5})
    assert peor["ajuste"] == -0.6                                      # acotado


def test_ajuste_acotado_y_real_solo_resta():
    d = {"insiders": {"compras_usd": 5e6, "ventas_usd": 0, "n_compradores": 3, "n_vendedores": 0},
         "analistas": {"subidas": 5, "bajadas": 0, "obj_sube": 5, "obj_baja": 0},
         "opciones": {"call_vol": 9000, "put_vol": 1000, "call_put": 9.0, "vol_oi": 3.0},
         "sector": {"rs5": 2, "rs20": 6}, "social": {"en_ranking": True, "menciones": 100, "ratio_24h": 4}}
    aj, partes = fx.ajuste(d, {"ajuste": 0.1})
    assert aj == fx.AJUSTE_MAX and set(partes) >= {"insiders", "analistas", "opciones", "sector", "social", "macro"}
    assert fx.scores_por_cuenta(7.0, aj) == (9.0, 7.0)                 # real no sube con fuentes nuevas
    assert fx.scores_por_cuenta(9.5, -0.7) == (8.8, 8.8)               # pero sí baja
    assert fx.scores_por_cuenta(9.5, 2.0) == (10.0, 9.5)
    assert fx.ajuste(None, None) == (0.0, {})


def test_una_fuente_que_falla_no_lanza():
    @fx.fuente("prueba_fallo")
    def rota():
        raise ValueError("boom")
    assert rota() is None
    assert fx.resumen_estado()["prueba_fallo"]["fallos"] == 1
    assert "boom" in fx.resumen_estado()["prueba_fallo"]["ultimo_error"]


def test_enriquecer_respeta_presupuesto(monkeypatch):
    def lento(t, precio=None, deadline=None):
        if t == "LENTO":
            time.sleep(3)
        return {"ok": t}
    monkeypatch.setattr(fx, "datos_ticker", lento)
    t0 = time.time()
    out = fx.enriquecer(["AAA", "LENTO", "BBB"], presupuesto_s=0.8)
    assert time.time() - t0 < 2.0
    assert set(out) == {"AAA", "BBB"}
    monkeypatch.setattr(fx, "ACTIVADA", False)
    assert fx.enriquecer(["AAA"]) == {}


def _cands():
    return [{"ticker": "AAA", "score": 7.0, "fuente": "dynamic_scanner_gha"},
            {"ticker": "BBB", "score_ajustado": 9.2, "score": 8.0, "fuente": "ew_pipeline_gha"},
            {"ticker": "CCC", "score": 2.0, "fuente": "x"}]


def test_market_open_integra_fuentes(monkeypatch):
    import market_open_execution as mo
    monkeypatch.setattr(fx, "macro_extra", lambda: {"regimen": "neutral", "ajuste": 0.0})
    monkeypatch.setattr(fx, "enriquecer", lambda tks, **k: {t: {"t": t} for t in tks})
    monkeypatch.setattr(fx, "ajuste", lambda d, m: ((1.5, {"insiders": 1.5}) if (d or {}).get("t") == "AAA"
                                                    else (-0.5, {"corto": -0.5}) if (d or {}).get("t") == "BBB"
                                                    else (0.0, {})))
    u = _cands()
    macro, _ = mo.aplicar_fuentes_extra(u, 6.0)
    a, b, c = u
    assert (a["_score_base"], a["_score_paper"], a["_score_real"]) == (7.0, 8.5, 7.0)
    assert (b["_score_base"], b["_score_paper"], b["_score_real"]) == (9.2, 8.7, 8.7)
    assert c["_score_paper"] == 2.0 and c["_partes"] == {}          # por debajo del umbral: no se enriquece
    assert macro["regimen"] == "neutral"


def test_market_open_si_fuentes_caen_opera_con_score_base(monkeypatch):
    import market_open_execution as mo

    def cae(*a, **k):
        raise RuntimeError("red caída")
    monkeypatch.setattr(fx, "macro_extra", cae)
    u = _cands()
    macro, estado = mo.aplicar_fuentes_extra(u, 6.0)
    assert macro is None and estado == {}
    assert [(c["_score_paper"], c["_score_real"], c["_ajuste"]) for c in u] == [(7.0, 7.0, 0.0), (9.2, 9.2, 0.0),
                                                                              (2.0, 2.0, 0.0)]
