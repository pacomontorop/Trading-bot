"""Tests offline del marcapasos (plan de franjas y franjas pendientes)."""
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("APCA_KEY", "PKTEST00000000000000")
os.environ.setdefault("APCA_SEC", "x")

import motor  # noqa: E402

AP = datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc)   # 9:30 ET (verano)
CI = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)    # 16:00 ET


def _horas(pl, wf):
    return [f"{t:%H:%M}" for w, t, _ in pl if w == wf]


def test_plan_dia_normal():
    pl = motor.plan(date(2026, 9, 11), AP, CI)
    assert _horas(pl, "market-open.yml") == ["13:35", "14:35", "15:35", "16:35", "17:35", "18:35"]
    assert _horas(pl, "selftest.yml") == ["13:10"]
    assert _horas(pl, "pre-close.yml") == ["19:40", "19:49"]
    assert _horas(pl, "intraday.yml")[0] == "13:35" and _horas(pl, "intraday.yml")[-1] == "19:35"
    assert _horas(pl, "kpi-report.yml") == ["21:30"] and _horas(pl, "ew-pipeline.yml") == ["21:40"]
    assert _horas(pl, "watchdog.yml")[0] == "13:20" and _horas(pl, "watchdog.yml")[-1] == "21:20"


def test_plan_media_sesion_y_festivo():
    ci = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)       # cierre anticipado 13:00 ET (invierno)
    ap = datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
    pl = motor.plan(date(2026, 11, 27), ap, ci)
    assert _horas(pl, "pre-close.yml") == ["17:40", "17:49"]
    assert all(t < ci for w, t, _ in pl if w == "intraday.yml")
    assert motor.plan(date(2026, 9, 7), None, None) == []            # Labor Day: nada


def test_pendientes_respeta_tolerancia():
    pl = motor.plan(date(2026, 9, 11), AP, CI)
    ahora = datetime(2026, 9, 11, 15, 50, tzinfo=timezone.utc)
    wfs = {w for w, _, _ in motor.pendientes(pl, ahora)}
    assert "market-open.yml" in wfs and "intraday.yml" in wfs and "selftest.yml" not in wfs
    tarde = datetime(2026, 9, 11, 16, 5, tzinfo=timezone.utc)          # 30 min después de la franja de 15:35
    assert ("market-open.yml", datetime(2026, 9, 11, 15, 35, tzinfo=timezone.utc), 25) not in motor.pendientes(pl, tarde)


def test_no_duplica_si_ya_hay_ejecucion(monkeypatch):
    llamadas = []
    monkeypatch.setattr(motor, "gh", lambda path, method="GET", data=None:
                        llamadas.append((method, path)) or (200, {"total_count": 1}))
    assert motor.ya_lanzado("market-open.yml", AP) is True
    monkeypatch.setattr(motor, "gh", lambda path, method="GET", data=None: (500, {}))
    assert motor.ya_lanzado("market-open.yml", AP) is True               # ante la duda, no duplicar
