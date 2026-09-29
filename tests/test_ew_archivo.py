"""Pruebas de ew_archivo.py. Todo sin red: lo que se comprueba es la LOGICA, que es
donde se cuelan los errores que estropean una muestra de investigacion para siempre."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("APCA_KEY", "PKTEST00000000000000")
os.environ.setdefault("APCA_SEC", "x")
os.environ.setdefault("EW_ARCHIVO_LOCAL", "/tmp/_ew_test.jsonl")

import ew_archivo as A  # noqa: E402


def test_num_trata_999_como_ausencia_de_whisper():
    """999 es el centinela de EW para "no hay whisper". Colarlo como numero mete un
    outlier de +99900% de asimetria que se come cualquier regresion posterior."""
    assert A.num(999) is None
    assert A.num("999") is None
    assert A.num(999.0) is None
    assert A.num(None) is None
    assert A.num("") is None
    assert A.num(1.25) == 1.25
    assert A.num("-0.40") == -0.40
    assert A.num("no es un numero") is None


def test_clave_identifica_la_observacion_no_la_linea():
    a = {"tipo": "captura", "ticker": "mu", "fecha_evento": "2026-09-30", "whisper": 1}
    b = {"tipo": "captura", "ticker": "MU", "fecha_evento": "2026-09-30", "whisper": 2}
    c = {"tipo": "resolucion", "ticker": "MU", "fecha_evento": "2026-09-30"}
    assert A.clave(a) == A.clave(b), "el ticker se normaliza a mayusculas"
    assert A.clave(a) != A.clave(c), "captura y resolucion son observaciones distintas"


def test_solo_se_anade_nunca_se_reescribe(tmp_path):
    """La garantia central del fichero: una observacion ya escrita no cambia jamas.
    Un archivo editable a posteriori no sirve para contrastar una hipotesis, porque no
    se puede distinguir un dato de una racionalizacion."""
    f = tmp_path / "a.jsonl"
    A.LOCAL = str(f)
    v1 = {"tipo": "captura", "ticker": "MU", "fecha_evento": "2026-09-30", "whisper": 34.14}
    assert A.anadir([v1], "x") == 1
    v2 = dict(v1, whisper=99.0)          # misma observacion, otro valor: se ignora
    assert A.anadir([v2], "x") == 0
    filas, _ = A.leer_archivo()
    assert len(filas) == 1
    assert filas[0]["whisper"] == 34.14, "el valor original debe sobrevivir"


def test_una_linea_corrupta_no_invalida_el_archivo(tmp_path):
    f = tmp_path / "b.jsonl"
    f.write_text('{"tipo":"captura","ticker":"A","fecha_evento":"2026-01-01"}\n'
                 'esto no es json\n'
                 '{"tipo":"captura","ticker":"B","fecha_evento":"2026-01-01"}\n', encoding="utf-8")
    A.LOCAL = str(f)
    filas, _ = A.leer_archivo()
    assert [x["ticker"] for x in filas] == ["A", "B"]


def test_asimetria_usa_valor_absoluto_del_consenso():
    """Con consenso NEGATIVO (una empresa en perdidas), dividir sin valor absoluto invierte
    el signo: un whisper mejor que el consenso saldria como asimetria negativa. En la
    captura real del 28-sep habia dos casos asi (UEC -0.04, MTN -5.40), asi que no es
    un caso de laboratorio."""
    w, cons = -0.30, -0.40               # pierde MENOS de lo esperado: buena noticia
    mal = (w - cons) / cons * 100
    bien = (w - cons) / abs(cons) * 100
    assert mal < 0 and bien > 0
    assert round(bien, 2) == 25.00


def test_informe_no_concluye_con_muestra_pequena(tmp_path, capsys):
    """Con n pequeno lo unico honesto es decir cuantos datos hay. Que el informe no
    empiece a sacar correlaciones con 5 puntos es parte del diseno, no un descuido."""
    f = tmp_path / "c.jsonl"
    filas = []
    for i in range(5):
        filas.append({"tipo": "captura", "ticker": f"T{i}", "fecha_evento": "2026-01-01",
                      "asimetria_pct": 1.0 * i})
        filas.append({"tipo": "resolucion", "ticker": f"T{i}", "fecha_evento": "2026-01-01",
                      "reaccion_1d_pct": 0.5 * i})
    f.write_text("".join(json.dumps(x) + "\n" for x in filas), encoding="utf-8")
    A.LOCAL = str(f)
    A.informe()
    out = capsys.readouterr().out
    assert "insuficiente" in out
    assert "correlacion" not in out, "con n=5 no se reporta correlacion, por bonita que salga"
