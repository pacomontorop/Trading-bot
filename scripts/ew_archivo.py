"""El archivo no pierde un resultado porque EW solo lo publique un día.

Contexto (14-oct-2026). `api/todaysresults` sirve ÚNICAMENTE la sesión en curso: no acepta
fecha y al día siguiente los de ayer ya no están. El resolutor lo leía en vivo y nada más,
así que:

  · Un evento de viernes se resuelve en la pasada del lunes, y para entonces EW ya no lo
    tiene. Medido sobre el archivo: 73 % de las resoluciones a D+1 traían `eps_real`;
    **0 de 6** a D+3, en dos lunes que no comparten nada más. DAL —29 analistas, la captura
    de más cobertura del archivo— se resolvió sin eps, sin consenso y sin whisper. Con ella
    HOVR, GLDG y CCEL.
  · Como el archivo es solo-añadir, esa fila vacía bloquea el reintento PARA SIEMPRE.
  · Y EW rellena el whisper con retraso: PENG daba 999 a las 22:10 del 7-oct y 0,84 unas
    horas después. El par se perdió por mirar una sola vez.

Lo que fijan estos tests:

  1. Cada día se guarda `todaysresults` crudo como líneas `resultado_ew`. Eso desacopla
     cuándo se mira de cuándo se resuelve, que es lo único que hace al archivo inmune a que
     una pasada se pierda.
  2. Una fila con mejor dato se AÑADE, no pisa a la anterior, y quien lee se queda con la
     mejor — no con la última.
  3. Sin nada de EW y con el evento reciente, NO se escribe: se espera. Escribir una fila
     muerta es peor que no escribir nada.
  4. Si EW da otra fecha de publicación, manda la de EW: NRIX se capturó para el 8-oct y
     reportó el 12, y se midió una ventana que no contenía el resultado.
"""
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("APCA_KEY", "PKTEST00000000000000")
os.environ.setdefault("APCA_SEC", "x")

import ew_archivo as A  # noqa: E402

SES = [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13),
       date(2026, 10, 14), date(2026, 10, 15)]
PRECIOS = [{"t": "2026-10-08T00:00:00Z", "c": 100.0}, {"t": "2026-10-09T00:00:00Z", "c": 100.0},
           {"t": "2026-10-12T00:00:00Z", "c": 100.0}, {"t": "2026-10-13T00:00:00Z", "c": 104.0},
           {"t": "2026-10-14T00:00:00Z", "c": 106.0}, {"t": "2026-10-15T00:00:00Z", "c": 108.0}]


def crudo(ticker, eps_date, whisper=None, eps=1.0, estimate=0.9):
    """Una fila tal y como la devuelve api/todaysresults (999 = sin whisper)."""
    return {"ticker": ticker, "epsDate": eps_date, "whisper": 999 if whisper is None else whisper,
            "eps": eps, "estimate": estimate, "earningsSurprise": 0.11,
            "revenueSurprise": 0.02, "highEstimate": 1.1, "lowEstimate": 0.7,
            "revenue": 100, "revenueEstimate": 98, "earningsGrowth": 0.2,
            "revenueGrowth": 0.1, "ewGrade": "B", "pwrRating": 3, "quarter": "Q3"}


def captura(ticker, fecha_evento, **kw):
    d = {"tipo": "captura", "ticker": ticker, "fecha_evento": fecha_evento,
         "whisper": None, "consenso": None, "asimetria_pct": None, "precio_captura": 100.0,
         "capturado_utc": "2026-10-07T22:10:00Z"}
    d.update(kw)
    return d


def montar(monkeypatch, filas, vivos=()):
    monkeypatch.setattr(A, "leer_archivo", lambda: (list(filas), None))
    monkeypatch.setattr(A, "ew_results_today", lambda: list(vivos))
    monkeypatch.setattr(A, "barras", lambda t, dias=40: (list(PRECIOS), "sip"))


# ── 1 · el banco ─────────────────────────────────────────────────────────────
def test_bancar_guarda_una_fila_por_resultado_con_lo_que_hace_falta():
    out = A.bancar_resultados([crudo("DAL", "2026-10-09T06:00:00", whisper=1.72)])
    assert len(out) == 1
    b = out[0]
    assert b["tipo"] == "resultado_ew" and b["ticker"] == "DAL"
    assert b["eps_date"] == "2026-10-09T06:00:00" and b["fecha_evento"] == "2026-10-09"
    assert b["whisper"] == 1.72 and b["consenso"] == 0.9 and b["eps"] == 1.0
    assert b["estimacion_alta"] == 1.1 and b["estimacion_baja"] == 0.7
    assert b["sorpresa_eps_pct"] == 0.11 and b["ew_grade"] == "B"


def test_el_centinela_999_se_guarda_como_sin_whisper():
    b = A.bancar_resultados([crudo("SAR", "2026-10-06T16:05:00")])[0]
    assert b["whisper"] is None, "999 es 'no hay whisper', no un whisper de 999"


def test_una_fila_sin_ticker_o_sin_fecha_no_se_banca():
    assert A.bancar_resultados([{"ticker": "", "epsDate": "2026-10-09T06:00:00"},
                                {"ticker": "X", "epsDate": ""}]) == []


# ── 2 · una fila mejorada se añade, no pisa ──────────────────────────────────
def test_la_clave_del_banco_deja_añadir_el_whisper_que_llega_tarde():
    """EL CASO PENG. 999 a las 22:10 y 0,84 unas horas después."""
    antes = {"tipo": "resultado_ew", "ticker": "PENG", "eps_date": "2026-10-06T16:05:00",
             "whisper": None, "eps": 0.84}
    despues = dict(antes, whisper=0.84)
    assert A.clave(antes) != A.clave(despues), \
        "si comparten clave, la fila buena se descarta por 'ya estaba' y el par se pierde"


def test_el_indice_prefiere_la_fila_que_trae_whisper():
    filas = [{"tipo": "resultado_ew", "ticker": "PENG", "eps_date": "2026-10-06T16:05:00",
              "whisper": None, "eps": 0.84, "consenso": 0.63, "visto_utc": "2026-10-07T22:10:00Z"},
             {"tipo": "resultado_ew", "ticker": "PENG", "eps_date": "2026-10-06T16:05:00",
              "whisper": 0.84, "eps": 0.84, "consenso": 0.63, "visto_utc": "2026-10-08T22:10:00Z"}]
    assert A.indice_resultados(filas)["PENG"]["whisper"] == 0.84
    assert A.indice_resultados(list(reversed(filas)))["PENG"]["whisper"] == 0.84, \
        "no puede depender del orden del fichero"


def test_el_indice_no_deja_que_un_duplicado_sin_whisper_tape_al_bueno():
    """EW devolvía PENG dos veces el 8-oct. Un dict ingenuo se queda con la última."""
    vivos = [crudo("PENG", "2026-10-06T16:05:00", whisper=0.84),
             crudo("PENG", "2026-10-06T16:05:00")]
    assert A.indice_resultados([], vivos)["PENG"]["whisper"] == 0.84


# ── 3 · la fuga de los viernes ───────────────────────────────────────────────
def test_resuelve_desde_el_banco_cuando_ew_ya_no_lo_sirve(monkeypatch):
    """EL CASO DAL. Evento el viernes, pasada el lunes, EW ya no lo tiene en vivo."""
    banco = A.bancar_resultados([crudo("DAL", "2026-10-09T06:00:00", whisper=1.72,
                                       eps=1.80, estimate=1.60)])
    montar(monkeypatch, [captura("DAL", "2026-10-09")] + banco, vivos=[])
    out = A.resolver(None, SES, date(2026, 10, 13), banco=banco, vivos=[])
    assert len(out) == 1
    r = out[0]
    assert r["eps_real"] == 1.80, "el eps estaba en el banco aunque EW ya no lo sirva"
    assert r["eps_whisper_ew"] == 1.72 and r["eps_consenso_ew"] == 1.60
    assert r["asimetria_pct"] == 7.5, "(1.72-1.60)/1.60*100"
    assert r["asimetria_origen"] == "todaysresults_post"
    assert r["reaccion_1d_pct"] is not None


def test_sin_banco_el_mismo_caso_se_perderia(monkeypatch):
    """Contraprueba: es el banco el que arregla esto, no otra cosa."""
    montar(monkeypatch, [captura("DAL", "2026-10-09")], vivos=[])
    assert A.resolver(None, SES, date(2026, 10, 13), banco=[], vivos=[]) == [], \
        "sin dato de EW y dentro del plazo de gracia, no se escribe"


# ── 4 · no escribir una fila muerta ──────────────────────────────────────────
def test_dentro_del_plazo_de_gracia_no_se_escribe_nada(monkeypatch):
    """Una fila vacía bloquea el reintento para siempre: mejor esperar."""
    montar(monkeypatch, [captura("HOVR", "2026-10-12")], vivos=[])
    assert A.resolver(None, SES, date(2026, 10, 13), banco=[], vivos=[]) == []


def test_pasado_el_plazo_se_escribe_aunque_no_haya_nada_de_ew(monkeypatch):
    """Si EW no lo publicó en 5 días no lo va a publicar: se guarda la reacción, que sí
    sirve de contexto, y se deja de reintentar."""
    montar(monkeypatch, [captura("GLDG", "2026-10-08")], vivos=[])
    out = A.resolver(None, SES, date(2026, 10, 15), banco=[], vivos=[])
    assert len(out) == 1
    assert out[0]["eps_real"] is None and out[0]["asimetria_pct"] is None
    assert out[0]["reaccion_1d_pct"] is not None


# ── 5 · la fecha de EW manda ─────────────────────────────────────────────────
def test_si_ew_movio_la_fecha_se_mide_la_ventana_correcta(monkeypatch):
    """EL CASO NRIX: capturado para el 8-oct, reportó el 12."""
    banco = A.bancar_resultados([crudo("NRIX", "2026-10-12T16:05:00", whisper=-0.50,
                                       eps=-0.48, estimate=-0.56)])
    montar(monkeypatch, [captura("NRIX", "2026-10-08")] + banco, vivos=[])
    out = A.resolver(None, SES, date(2026, 10, 14), banco=banco, vivos=[])
    assert len(out) == 1
    r = out[0]
    assert r["fecha_evento"] == "2026-10-08", "la captura no se reescribe"
    assert r["fecha_evento_ew"] == "2026-10-12", "y queda dicho cuál era la buena"
    assert r["precio_base"] == 100.0, "base = cierre del 12, no del 8"
    assert r["reaccion_1d_pct"] == 4.0, "12 -> 13: 100 -> 104"


def test_si_la_fecha_de_ew_aun_no_ha_llegado_no_se_resuelve(monkeypatch):
    banco = A.bancar_resultados([crudo("X", "2026-10-15T16:05:00", whisper=1.0)])
    montar(monkeypatch, [captura("X", "2026-10-08")] + banco, vivos=[])
    assert A.resolver(None, SES, date(2026, 10, 14), banco=banco, vivos=[]) == []


def test_la_fecha_normal_no_se_toca(monkeypatch):
    banco = A.bancar_resultados([crudo("LW", "2026-10-12T08:00:00", whisper=0.72,
                                       eps=0.80, estimate=0.59)])
    montar(monkeypatch, [captura("LW", "2026-10-12")] + banco, vivos=[])
    r = A.resolver(None, SES, date(2026, 10, 14), banco=banco, vivos=[])[0]
    assert r["fecha_evento_ew"] is None, "solo se anota cuando de verdad se movió"


# ── 6 · la captura ex-ante sigue mandando ────────────────────────────────────
def test_la_captura_ex_ante_gana_al_banco(monkeypatch):
    banco = A.bancar_resultados([crudo("MU", "2026-10-12T16:05:00", whisper=34.14,
                                       eps=33.42, estimate=31.41)])
    cap = captura("MU", "2026-10-12", whisper=34.2, consenso=31.4, asimetria_pct=8.9)
    montar(monkeypatch, [cap] + banco, vivos=[])
    r = A.resolver(None, SES, date(2026, 10, 14), banco=banco, vivos=[])[0]
    assert r["asimetria_pct"] == 8.9 and r["asimetria_origen"] == "captura_exante"
    assert r["coincide_exante"] is False, "8,9 frente a 8,69: EW lo movió, y hay que saberlo"


# ── 7 · los campos que se estaban tirando ────────────────────────────────────
def test_la_captura_guarda_el_movimiento_tipico_de_la_accion(monkeypatch):
    """`avgEPSMove` y `expectedMove` permiten normalizar la reacción por lo que esa acción
    se mueve de normal. Sin eso, la varianza entre valores se come la señal."""
    monkeypatch.setattr(A, "leer_archivo", lambda: ([], None))
    monkeypatch.setattr(A, "ew_calendar", lambda d: [{
        "ticker": "UNH", "company": "UnitedHealth", "releaseTime": 1, "total": 70,
        "q1EstEPS": 4.12, "whisper": None, "avgEPSMove": 5.4, "expectedMove": 6.1,
        "avgSent": 0.62, "score": 7.0, "marketCap": 3.2e11, "direction": "up",
        "epsImpact": 0.8}])
    monkeypatch.setattr(A, "ew_stock", lambda t: None)
    monkeypatch.setattr(A, "barras", lambda t, dias=40: (list(PRECIOS), "sip"))

    c = A.capturar(None, [date(2026, 10, 13)])[0]
    assert c["consenso"] == 4.12 and c["consenso_origen"] == "caldata"
    assert c["mov_medio_eps"] == 5.4 and c["mov_esperado_pct"] == 6.1
    assert c["capitalizacion"] == 3.2e11 and c["sentimiento_medio"] == 0.62
    assert c["score_ew"] == 7.0 and c["direccion_ew"] == "up" and c["impacto_eps"] == 0.8


# ── 8 · aritmética de la asimetría (del arreglo del 5-oct, sin tests en el repo) ──
def test_consenso_negativo_no_invierte_el_signo(monkeypatch):
    """Empresa en pérdidas: dividir sin valor absoluto convierte una mejora en caída."""
    banco = A.bancar_resultados([crudo("UEC", "2026-10-12T16:05:00", whisper=-0.04,
                                       eps=-0.07, estimate=-0.05)])
    montar(monkeypatch, [captura("UEC", "2026-10-12")] + banco, vivos=[])
    r = A.resolver(None, SES, date(2026, 10, 14), banco=banco, vivos=[])[0]
    assert r["asimetria_pct"] == 20.0, \
        f"el whisper es MEJOR que el consenso: positiva, y salió {r['asimetria_pct']}"


def test_consenso_cero_no_revienta_la_division(monkeypatch):
    banco = A.bancar_resultados([crudo("X0", "2026-10-12T16:05:00", whisper=0.10,
                                       eps=0.1, estimate=0.0)])
    montar(monkeypatch, [captura("X0", "2026-10-12")] + banco, vivos=[])
    assert A.resolver(None, SES, date(2026, 10, 14), banco=banco, vivos=[])[0]["asimetria_pct"] is None


# ── 9 · el informe mira el caudal, no el depósito ────────────────────────────
def test_el_informe_avisa_si_se_rompe_caldata(monkeypatch, capsys):
    """La avería que SÍ deja el contador a cero: capturas sin consenso."""
    hoy = A.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(A, "leer_archivo", lambda: ([
        {"tipo": "captura", "ticker": "LEVI", "fecha_evento": "2026-10-16",
         "consenso": None, "whisper": None, "asimetria_pct": None, "capturado_utc": hoy},
    ], None))
    A.informe()
    txt = capsys.readouterr().out
    assert "caldata esta rota" in txt
    assert "1 capturas hoy, 0 con consenso" in txt


def test_el_informe_avisa_si_el_banco_vuelve_sin_whisper(monkeypatch, capsys):
    hoy = A.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(A, "leer_archivo", lambda: ([
        {"tipo": "captura", "ticker": "LEVI", "fecha_evento": "2026-10-16",
         "consenso": 0.36, "capturado_utc": hoy},
        {"tipo": "resultado_ew", "ticker": "SAR", "eps_date": "2026-10-13T16:05:00",
         "whisper": None, "visto_utc": hoy},
    ], None))
    A.informe()
    assert "todaysresults ha dejado de servir" in capsys.readouterr().out


def test_el_informe_NO_avisa_cuando_todo_va_bien(monkeypatch, capsys):
    """La trampa que se arregla aquí: la alarma vieja saltaba todos los días aunque el
    archivo estuviese funcionando, porque medía algo que vale 0 por diseño."""
    hoy = A.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(A, "leer_archivo", lambda: ([
        {"tipo": "captura", "ticker": "UNH", "fecha_evento": "2026-10-16",
         "consenso": 4.12, "whisper": None, "asimetria_pct": None, "capturado_utc": hoy},
        {"tipo": "resultado_ew", "ticker": "DAL", "eps_date": "2026-10-13T06:00:00",
         "whisper": 1.72, "visto_utc": hoy},
    ], None))
    A.informe()
    assert "AVISO" not in capsys.readouterr().out


def test_el_informe_cuenta_el_banco(monkeypatch, capsys):
    hoy = A.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(A, "leer_archivo", lambda: ([
        {"tipo": "resultado_ew", "ticker": "DAL", "eps_date": "2026-10-09T06:00:00",
         "whisper": 1.72, "visto_utc": hoy},
        {"tipo": "resultado_ew", "ticker": "SAR", "eps_date": "2026-10-06T16:05:00",
         "whisper": None, "visto_utc": "2026-10-06T22:10:00Z"},
    ], None))
    A.informe()
    assert "banco 1 resultados hoy, 1 con whisper (2 filas en total)" in capsys.readouterr().out
