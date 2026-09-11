#!/usr/bin/env python3
"""
motor.py — marcapasos del sistema (2026-09-11).

Por qué: el cron de GitHub Actions es "best effort". En este repo llega con 1-2 h de retraso
o no llega (11-sep: 0 ejecuciones programadas durante toda la sesión). Un sistema que opera
no puede depender de eso.

Qué hace: vive dentro de un job (≤ VIDA_S), mira cada minuto el calendario del mercado
(Alpaca) y lanza por workflow_dispatch cada tarea en su franja, SIN duplicar: antes de lanzar
comprueba en la API si ese workflow ya tiene una ejecución creada desde el inicio de la franja
(sea del cron, manual o del propio motor). Antes de agotar su tiempo lanza su relevo
(motor.yml); el grupo de concurrencia garantiza que nunca hay dos motores a la vez.
Los cron de cada workflow siguen como respaldo.

Nunca decide operaciones: solo arranca los workflows existentes, que aplican sus propias
reglas y límites (risk_limits.json).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REPO = os.environ.get("GITHUB_REPOSITORY", "pacomontorop/Trading-bot")
TOKEN = os.environ.get("DISPATCH_TOKEN", "")
VIDA_S = int(os.environ.get("MOTOR_VIDA_S", str(5 * 3600 + 20 * 60)))
DRY = os.environ.get("MOTOR_DRY", "0") == "1"
PASO_S = 60


def log(m: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {m}", flush=True)


# ── Plan del día (función pura, testeada) ────────────────────────────────────
def plan(dia: date, apertura: datetime | None, cierre: datetime | None) -> list:
    """[(workflow, inicio_franja_utc, tolerancia_min)] para un día. Sin sesión → solo nada."""
    if not apertura or not cierre:
        return []
    m = lambda base, mins: base + timedelta(minutes=mins)
    out = [("selftest.yml", m(apertura, -20), 40)]
    t = m(apertura, 5)                                   # market-open: +5 min y cada hora (ventana 3-330)
    while t <= m(apertura, 330):
        out.append(("market-open.yml", t, 25))
        t = m(t, 60)
    t = m(apertura, 5)                                   # intraday: cada 30 min en sesión
    while t < cierre:
        out.append(("intraday.yml", t, 20))
        t = m(t, 30)
    t = apertura.replace(minute=20, second=0, microsecond=0)   # watchdog: cada hora :20 hasta cierre+2 h
    while t <= m(cierre, 120):
        out.append(("watchdog.yml", t, 30))
        t = m(t, 60)
    out += [("pre-close.yml", m(cierre, -20), 8), ("pre-close.yml", m(cierre, -11), 5),
            ("kpi-report.yml", m(cierre, 90), 120), ("ew-pipeline.yml", m(cierre, 100), 120)]
    return sorted(out, key=lambda x: x[1])


def pendientes(pl: list, ahora: datetime) -> list:
    """Franjas que tocan ahora (dentro de su tolerancia)."""
    return [(wf, ini, tol) for wf, ini, tol in pl if ini <= ahora < ini + timedelta(minutes=tol)]


# ── GitHub API ───────────────────────────────────────────────────────────────
def gh(path: str, method: str = "GET", data: dict | None = None):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}", method=method,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"_error": e.read().decode(errors="replace")[:200]}
    except Exception as e:  # noqa: BLE001
        return 0, {"_error": str(e)[:200]}


def ya_lanzado(wf: str, desde: datetime) -> bool:
    """True si ese workflow tiene alguna ejecución creada desde `desde` (cron, manual o motor)."""
    st, d = gh(f"/actions/workflows/{wf}/runs?created=>={(desde - timedelta(minutes=2)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
               f"&per_page=1")
    if st != 200:
        log(f"  ⚠️ no se pudo consultar {wf} (HTTP {st}); por prudencia se asume lanzado")
        return True
    return int(d.get("total_count") or 0) > 0


def lanzar(wf: str) -> bool:
    if DRY:
        log(f"  [MOTOR_DRY] lanzaría {wf}"); return True
    st, d = gh(f"/actions/workflows/{wf}/dispatches", "POST", {"ref": "main"})
    log(f"  → lanzado {wf}: HTTP {st} {d.get('_error', '') if isinstance(d, dict) else ''}")
    return st == 204


# ── Calendario (Alpaca) ──────────────────────────────────────────────────────
_cal: dict = {}


def sesion(dia: date):
    """(apertura_utc, cierre_utc) o (None, None) si no hay sesión. Si Alpaca falla: horario estándar L-V."""
    if dia in _cal:
        return _cal[dia]
    res = (None, None)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from common import paper_client
        c = paper_client().req(f"/calendar?start={dia.isoformat()}&end={dia.isoformat()}")
        if isinstance(c, list):
            if c and c[0].get("date") == dia.isoformat():
                o = datetime.fromisoformat(f"{dia}T{c[0]['open']}").replace(tzinfo=ET)
                k = datetime.fromisoformat(f"{dia}T{c[0]['close']}").replace(tzinfo=ET)
                res = (o.astimezone(timezone.utc), k.astimezone(timezone.utc))
            _cal[dia] = res
            return res
        raise RuntimeError(str(c)[:120])
    except Exception as e:  # noqa: BLE001 — sin calendario: L-V 9:30-16:00 ET (los scripts vuelven a comprobar)
        log(f"  ⚠️ calendario Alpaca no disponible ({e}); se usa horario estándar")
        if dia.weekday() < 5:
            o = datetime.combine(dia, datetime.min.time(), ET).replace(hour=9, minute=30)
            res = (o.astimezone(timezone.utc), o.replace(hour=16, minute=0).astimezone(timezone.utc))
        _cal[dia] = res
        return res


def main():
    t0 = time.time()
    log(f"=== motor · vida {VIDA_S // 60} min · repo {REPO} {'[MOTOR_DRY]' if DRY else ''} ===")
    if not TOKEN:
        log("Sin DISPATCH_TOKEN: nada que hacer."); return
    hechos: set = set()
    try:
        bucle(t0, hechos)
    finally:                                   # pase lo que pase, la cadena continúa
        log("Fin de vida del job → se lanza el relevo (motor.yml).")
        lanzar("motor.yml")


def bucle(t0: float, hechos: set) -> None:
    while True:
        ahora = datetime.now(timezone.utc)
        dia = ahora.astimezone(ET).date()
        try:
            ap, ci = sesion(dia)
            for wf, ini, _tol in pendientes(plan(dia, ap, ci), ahora):
                clave = (wf, ini)
                if clave in hechos:
                    continue
                if ya_lanzado(wf, ini):
                    hechos.add(clave)
                    log(f"  {wf} franja {ini:%H:%M}Z ya tiene ejecución; no se duplica")
                elif lanzar(wf):                   # si el lanzamiento falla se reintenta el minuto siguiente
                    hechos.add(clave)
        except Exception as e:  # noqa: BLE001 — el motor nunca se cae por un fallo puntual
            log(f"  ⚠️ error en el ciclo: {type(e).__name__}: {e}")
        if time.time() - t0 > VIDA_S:
            return
        time.sleep(PASO_S)


if __name__ == "__main__":
    main()
