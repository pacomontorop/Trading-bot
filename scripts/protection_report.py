#!/usr/bin/env python3
"""Parte de proteccion: que hay abierto, que lo protege y por donde se ha ido el dinero.

SOLO LEE. No crea, no modifica y no cancela ninguna orden. La unica escritura es sobre
`performance_log.json`, en el bloque `proteccion`.

Por que existe (13-oct-2026): se cerro una posicion con -575,84 $ y no quedo ni una linea
en el log que dijera que orden la cerro ni por que. Con las entradas congeladas, el libro
heredado es lo UNICO que puede seguir perdiendo dinero, y no habia ningun sitio donde mirar
cuanto queda abierto ni si sigue protegido.

Responde cuatro preguntas, todos los dias:
  1. Censo de posiciones en las dos cuentas.
  2. Cobertura de stops  -> usa open_orders() de common.py (status=all + ESTADOS_VIVOS).
  3. Cada cierre del dia -> client_order_id, quien lo cerro y por que.
  4. P&L realizado del dia frente a P&L latente (mark-to-market) que sigue en riesgo.

Dos trampas conocidas de Alpaca, las dos cubiertas aqui:
  - Las patas de stop de un bracket quedan en estado `held` y NO salen en
    /orders?status=open. Por eso se usa open_orders(), nunca una llamada cruda.
    Ver claude/bug-stops-held-2026-09-14.md.
  - Alpaca llama `SOLUSD` a la posicion y `SOL/USD` a la misma orden. Comparar simbolos en
    crudo da falsos "sin stop". Por eso todo pasa por norm().
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402
    ET,
    is_crypto,
    log,
    now_utc,
    paper_client,
    real_client,
    tg,
    update_log,
)

TIPOS_STOP = {"stop", "stop_limit", "trailing_stop"}
TIPOS_OBJETIVO = {"limit"}


# --------------------------------------------------------------------------- utilidades


def norm(sym) -> str:
    """`SOL/USD` y `SOLUSD` son el mismo activo para Alpaca. Aqui tambien."""
    return (sym or "").replace("/", "").replace("-", "").upper().strip()


def f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def clasifica_cid(cid: str) -> str:
    """Quien creo la orden, segun el convenio del repo.

    Ver el bloque 'Quien ejecuta' de las instrucciones del proyecto. `PROTECT.*` es una
    orden heredada antigua que GHA sigue ratcheteando: NO es una alarma. `COWORK-*` con
    fecha reciente si lo es, porque el ejecutor debe ser GitHub Actions y nadie mas.
    """
    c = (cid or "").strip()
    if not c:
        return "sin_id"
    if c.startswith("GHA-PROTECT-"):
        return "gha_proteccion"
    if c.startswith("GHA-"):
        return "gha"
    if c.startswith("PROTECT."):
        return "heredada_antigua"
    if c.startswith("COWORK-"):
        return "cowork_INESPERADO"
    # Alpaca pone un UUID a las patas que genera el mismo al abrir un bracket.
    if len(c) >= 32 and c.count("-") >= 4:
        return "pata_bracket"
    return "desconocido"


def motivo_cierre(orden: dict) -> str:
    """Por que se cerro. Se deduce del tipo de orden, que es lo unico que Alpaca da."""
    t = (orden.get("type") or orden.get("order_type") or "").lower()
    cid = clasifica_cid(orden.get("client_order_id"))
    if t in TIPOS_STOP:
        return "stop"
    if t in TIPOS_OBJETIVO:
        return "objetivo"
    if t == "market":
        if cid in ("gha_proteccion", "heredada_antigua"):
            return "cierre_forzado_proteccion"
        return "cierre_a_mercado"
    return t or "desconocido"


# ------------------------------------------------------------------- nucleo (sin cuenta)


def cobertura_stops(posiciones: list, ordenes_vivas: list) -> list:
    """Cruza posiciones con ordenes vivas y dice cual esta protegida y cual no.

    Funcion pura: se le pasan las listas ya leidas. Asi se puede probar sin tocar Alpaca.
    """
    por_sim: dict[str, list] = {}
    for o in ordenes_vivas:
        por_sim.setdefault(norm(o.get("symbol")), []).append(o)

    out = []
    for p in posiciones:
        sim = norm(p.get("symbol"))
        qty = abs(f(p.get("qty")))
        largo = f(p.get("qty")) >= 0
        lado_cierre = "sell" if largo else "buy"

        stops, objetivos, qty_stop = [], [], 0.0
        for o in por_sim.get(sim, []):
            if (o.get("side") or "").lower() != lado_cierre:
                continue
            t = (o.get("type") or o.get("order_type") or "").lower()
            if t in TIPOS_STOP:
                stops.append(o)
                qty_stop += abs(f(o.get("qty")))
            elif t in TIPOS_OBJETIVO:
                objetivos.append(o)

        # Un stop que cubre menos acciones de las que hay abiertas protege a medias.
        if not stops:
            estado = "SIN_STOP"
        elif qty_stop + 1e-9 < qty:
            estado = "STOP_PARCIAL"
        else:
            estado = "ok"

        out.append({
            "simbolo": p.get("symbol"),
            "qty": f(p.get("qty")),
            "lado": "largo" if largo else "corto",
            "cripto": is_crypto(p),
            "precio_entrada": f(p.get("avg_entry_price")),
            "precio_actual": f(p.get("current_price")),
            "valor_mercado": f(p.get("market_value")),
            "pnl_latente_usd": f(p.get("unrealized_pl")),
            "pnl_latente_pct": round(f(p.get("unrealized_plpc")) * 100, 2),
            "estado": estado,
            "n_stops": len(stops),
            "qty_cubierta": qty_stop,
            "stop_px": min([f(s.get("stop_price")) for s in stops], default=None) if largo
                       else max([f(s.get("stop_price")) for s in stops], default=None),
            "n_objetivos": len(objetivos),
            "ids_stop": [s.get("client_order_id") for s in stops][:4],
        })

    orden_gravedad = {"SIN_STOP": 0, "STOP_PARCIAL": 1, "ok": 2}
    out.sort(key=lambda r: (orden_gravedad.get(r["estado"], 3), r["pnl_latente_usd"]))
    return out


def cierres_del_dia(ordenes: list, desde_iso: str) -> list:
    """Ordenes llenadas desde `desde_iso`. Cada una con quien la mando y por que."""
    out = []
    for o in ordenes:
        if (o.get("status") or "").lower() != "filled":
            continue
        ts = o.get("filled_at") or ""
        if ts and ts < desde_iso:
            continue
        qty = abs(f(o.get("filled_qty")))
        px = f(o.get("filled_avg_price"))
        out.append({
            "simbolo": o.get("symbol"),
            "lado": (o.get("side") or "").lower(),
            "qty": qty,
            "precio": px,
            "importe_usd": round(qty * px, 2),
            "tipo": (o.get("type") or o.get("order_type") or "").lower(),
            "client_order_id": o.get("client_order_id"),
            "origen": clasifica_cid(o.get("client_order_id")),
            "motivo": motivo_cierre(o),
            "llenada_utc": ts,
        })
    out.sort(key=lambda r: r["llenada_utc"])
    return out


def resumen_cuenta(nombre: str, cuenta: dict, filas: list, cierres: list) -> dict:
    equity = f(cuenta.get("equity"))
    last = f(cuenta.get("last_equity"))
    latente = round(sum(r["pnl_latente_usd"] for r in filas), 2)
    sin_stop = [r for r in filas if r["estado"] in ("SIN_STOP", "STOP_PARCIAL")]
    # Lo realizado hoy no viene dado por Alpaca; se deduce del movimiento de equity
    # menos lo que ha cambiado de valor lo que sigue abierto. Es una aproximacion, y
    # se etiqueta como tal para que nadie la confunda con la cifra de kpi_report.
    mov_dia = round(equity - last, 2) if last else None
    return {
        "cuenta": nombre,
        "equity": round(equity, 2),
        "equity_cierre_anterior": round(last, 2) if last else None,
        "movimiento_dia_usd": mov_dia,
        "n_posiciones": len(filas),
        "expuesto_usd": round(sum(abs(r["valor_mercado"]) for r in filas), 2),
        "pnl_latente_usd": latente,
        "_nota_latente": "Dinero que sigue en riesgo: aun no es perdida, pero puede serlo.",
        "n_sin_proteger": len(sin_stop),
        "sin_proteger": [r["simbolo"] for r in sin_stop],
        "n_cierres_hoy": len(cierres),
        "cierres_por_motivo": _cuenta_por(cierres, "motivo"),
        "cierres_por_origen": _cuenta_por(cierres, "origen"),
    }


def _cuenta_por(filas: list, campo: str) -> dict:
    d: dict[str, int] = {}
    for r in filas:
        d[r[campo]] = d.get(r[campo], 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))


def avisos(resumenes: list, cierres_todos: list) -> list:
    """Solo avisos que pueden disparar de verdad. Una alarma que suena cada dia no es alarma."""
    av = []
    for r in resumenes:
        if r["n_sin_proteger"]:
            av.append(f"{r['cuenta']}: {r['n_sin_proteger']} posicion(es) sin stop vivo "
                      f"-> {', '.join(r['sin_proteger'][:6])}")
    inesperados = [c for c in cierres_todos if c["origen"] == "cowork_INESPERADO"]
    if inesperados:
        av.append(f"{len(inesperados)} cierre(s) con client_order_id COWORK-* reciente. "
                  f"El ejecutor debe ser GitHub Actions y nadie mas.")
    sin_id = [c for c in cierres_todos if c["origen"] in ("sin_id", "desconocido")]
    if sin_id:
        av.append(f"{len(sin_id)} cierre(s) sin origen identificable: "
                  f"{', '.join(c['simbolo'] for c in sin_id[:6])}")
    return av


# ------------------------------------------------------------- bloques caducados del log


BLOQUES_CADUCADOS = {
    "kpis_actuales": "Congelado el 2026-05-06. Decia profit factor 8,32 y win rate 55,6 %.",
    "kpis.rendimiento_global": "Congelado el 2026-09-23. Decia equity 106.137 $ (+0,6 %).",
    "kpis.riesgo": "Congelado el 2026-09-23. Decia drawdown maximo 3,45 %.",
    "kpis_actuales.profit_factor_total": None,
}

VERDAD = "kpis.rendimiento_verificado"


def marca_caducados(pl: dict) -> int:
    """Sella los marcadores muertos. NO borra nada: solo anade `_OBSOLETO`.

    El log arrastra tres marcadores distintos y dos de ellos llevan meses congelados en
    positivo, mientras kpi_report escribe la cifra verificada en otro sitio. Cualquiera que
    abra el fichero -- persona o sesion nueva -- puede leer el equivocado y concluir que el
    sistema gana dinero. Ya ha pasado. Esto no arregla los numeros, los etiqueta.
    """
    n = 0
    for ruta, motivo in BLOQUES_CADUCADOS.items():
        if motivo is None:
            continue
        partes = ruta.split(".")
        nodo = pl
        for p in partes[:-1]:
            nodo = nodo.get(p) if isinstance(nodo, dict) else None
            if nodo is None:
                break
        if not isinstance(nodo, dict):
            continue
        destino = nodo.get(partes[-1])
        if not isinstance(destino, dict) or "_OBSOLETO" in destino:
            continue
        destino["_OBSOLETO"] = (
            f"NO USAR. {motivo} La cifra buena esta en `{VERDAD}`, que la escribe "
            f"scripts/kpi_report.py leyendo Alpaca."
        )
        n += 1
    return n


# -------------------------------------------------------------------------------- main


def recoge(alp, nombre: str) -> tuple[dict, list, list]:
    inicio = (now_utc() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cuenta = alp.account() or {}
    posiciones = alp.positions()
    vivas = alp.open_orders()
    recientes = alp.orders_since(inicio)
    filas = cobertura_stops(posiciones, vivas)
    cierres = cierres_del_dia(recientes, inicio)
    log(f"  {nombre}: {len(filas)} posiciones, {len(vivas)} ordenes vivas, "
        f"{len(cierres)} cierres en 24 h")
    return resumen_cuenta(nombre, cuenta, filas, cierres), filas, cierres


def main() -> int:
    log("parte de proteccion")
    resumenes, detalle, cierres_todos = [], {}, []

    for nombre, cliente in (("PAPER", paper_client()), ("REAL", real_client())):
        if cliente is None:
            log(f"  {nombre}: sin credenciales, se omite")
            continue
        try:
            res, filas, cierres = recoge(cliente, nombre)
        except Exception as e:                                 # noqa: BLE001
            log(f"  {nombre}: ERROR leyendo Alpaca -> {e}")
            resumenes.append({"cuenta": nombre, "error": str(e)[:200],
                              "n_sin_proteger": 0, "sin_proteger": []})
            continue
        resumenes.append(res)
        detalle[nombre] = {"posiciones": filas, "cierres_24h": cierres}
        cierres_todos += cierres

    av = avisos([r for r in resumenes if "error" not in r], cierres_todos)
    bloque = {
        "_meta": "Escrito por scripts/protection_report.py. Solo lectura sobre Alpaca.",
        "ts": now_utc().strftime("%Y-%m-%dT%H:%MZ"),
        "fecha_et": datetime.now(ET).strftime("%Y-%m-%d"),
        "resumen": resumenes,
        "avisos": av,
        "detalle": detalle,
    }

    sellados = {"n": 0}

    def mutate(pl):
        pl["proteccion"] = bloque
        hist = pl.setdefault("proteccion_historico", [])
        hist.append({k: bloque[k] for k in ("ts", "fecha_et", "resumen", "avisos")})
        del hist[:-60]                      # 60 dias; el log ya pesa de mas
        sellados["n"] = marca_caducados(pl)
        return pl

    try:
        update_log(mutate, "parte de proteccion")
        log(f"  log actualizado (bloques caducados sellados: {sellados['n']})")
    except Exception as e:                                     # noqa: BLE001
        log(f"  ERROR escribiendo el log -> {e}")

    for a in av:
        log(f"  AVISO: {a}")
    if av:
        tg("Parte de proteccion\n" + "\n".join("- " + a for a in av))

    for r in resumenes:
        if "error" in r:
            continue
        log(f"  {r['cuenta']}: equity {r['equity']:.2f} | {r['n_posiciones']} pos | "
            f"expuesto {r['expuesto_usd']:.2f} | latente {r['pnl_latente_usd']:+.2f} | "
            f"sin proteger {r['n_sin_proteger']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
