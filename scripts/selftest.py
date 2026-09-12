#!/usr/bin/env python3
"""
Selftest — GitHub Actions (v1, 2026-09-10). Demuestra que el sistema puede operar solo.

Modo "basic" (diario, 13:10 UTC, antes de la apertura):
  1. Credenciales paper y real válidas, cuentas ACTIVE y sin bloqueo.
  2. Datos de mercado y TODAS las fuentes (EarningsWhispers, Nasdaq, Alpaca noticias, Yahoo).
  3. Orden real de prueba en PAPER: bracket buy limit a -50 % (no puede llenarse),
     con patas TP/SL → se verifica que Alpaca la acepta con sus 2 patas → se cancela.
  4. Lectura y escritura del performance_log en GitHub (campo "selftest").
  5. Aviso por Telegram.

Fallo DURO (sale con código 1 y GitHub avisa por email): cuentas no operativas, Alpaca
rechaza el bracket de prueba, no se puede leer/escribir el log, o TODAS las fuentes caídas.
Fallo BLANDO (⚠️ en el aviso, run en verde): una fuente suelta caída o sin datos todavía.
Corre 20 min ANTES de la apertura: a esa hora el mercado está cerrado y IEX puede no tener
ninguna operación del día, así que eso no es una avería y no debe despertar a nadie.
Modo "full" (manual, con mercado abierto): además compra 1 acción en PAPER con
  bracket, espera el fill, sube el stop con PATCH (la misma operación que usa el
  intraday monitor) y cierra la posición. Sus fills se excluyen de los KPIs.

En la cuenta REAL no se envía ninguna orden de prueba: solo comprobaciones de lectura.
Sale con código 1 si algo falla (GitHub marca el run en rojo y avisa por email).
"""
import os
import sys
import time

from common import (DRY_RUN, log, now_utc, paper_client, place_bracket_entry, real_client, round_px,
                    tg, update_log, wait_fill_or_cancel)

SYM = os.environ.get("SELFTEST_SYMBOL", "F")
MODE = os.environ.get("SELFTEST_MODE", "basic")


def main():
    ahora = now_utc()
    tag = ahora.strftime("%Y%m%d%H%M")
    P, R = paper_client(), real_client()
    ok, fail, warn = [], [], []

    def check(name, cond, detail="", duro=True):
        txt = f"{name}{(' — ' + detail) if detail else ''}"
        (ok if cond else (fail if duro else warn)).append(txt)
        log(f"{'✅' if cond else ('❌' if duro else '⚠️')} {name} {detail}")
        return cond

    # 1 · cuentas
    for alp in (P, R):
        if alp is None:
            check("REAL credenciales", False, "faltan APCA_KEY_R/APCA_SEC_R"); continue
        a = alp.account()
        good = "_error" not in a and a.get("status") == "ACTIVE" and not a.get("trading_blocked") \
            and not a.get("account_blocked")
        check(f"{alp.name} cuenta operativa", good,
              f"equity ${float(a.get('equity', 0)):,.2f} BP ${float(a.get('buying_power', 0)):,.2f}"
              if "_error" not in a else a["_error"][:80])

    # 2 · datos
    clock = P.clock()
    # Vale cualquier respuesta con forma de reloj: 20 min antes de la apertura is_open es False
    # y eso es lo correcto, no un fallo. Solo es fallo que la API no conteste.
    check("Reloj de mercado", bool(clock) and ("is_open" in clock or "next_open" in clock),
          f"abierto={clock.get('is_open')} próxima={str(clock.get('next_open'))[:16]}")
    px = P.ref_price(SYM)
    check(f"Precio {SYM}", bool(px), f"{px}" + ("" if P.latest_price(SYM) else " (horquilla/cierre previo: pre-market sin operaciones)"),
          duro=False)

    # 2b · todas las fuentes de datos (EarningsWhispers, Nasdaq, Alpaca, Yahoo)
    try:
        from datasources import health_check
        salud = health_check()
        for k, (good, det) in salud.items():
            check(f"Fuente {k}", good, det, duro=False)      # una fuente suelta no bloquea: hay respaldos
        vivas = sum(1 for good, _ in salud.values() if good)
        check("Fuentes de datos (al menos una viva)", vivas > 0, f"{vivas}/{len(salud)} OK")
    except Exception as e:
        check("Fuentes de datos", False, str(e)[:100])

    # 3 · orden de prueba (no llenable)
    if px:
        lim = round_px(px * 0.5)
        lv = {"stop": round_px(lim * 0.95), "stop_limit": round_px(lim * 0.95 * 0.995), "tp": round_px(lim * 1.10)}
        r = place_bracket_entry(P, SYM, 1, lim, lv, f"SELFTEST-B-{tag}")
        if check("PAPER envío bracket", "_error" not in r, r.get("_error", "")[:100] or f"id {str(r.get('id'))[:8]}"):
            if not DRY_RUN:
                o = P.req(f"/orders/{r['id']}?nested=true")
                check("PAPER bracket con patas TP+SL", len(o.get("legs") or []) == 2, f"estado {o.get('status')}")
                P.req(f"/orders/{r['id']}", "DELETE")
                st = None
                for _ in range(6):
                    time.sleep(2)
                    st = P.req(f"/orders/{r['id']}").get("status")
                    if st in ("canceled", "pending_cancel"):
                        break
                check("PAPER cancelación", st in ("canceled", "pending_cancel"), f"estado {st}")

    # 3b · ciclo completo (solo manual y con mercado abierto)
    if MODE == "full":
        if not clock.get("is_open"):
            check("FULL: mercado abierto", False, "ejecuta el modo full en horario de mercado")
        elif px:
            lv = {"stop": round_px(px * 0.97), "stop_limit": round_px(px * 0.97 * 0.995), "tp": round_px(px * 1.06)}
            r = place_bracket_entry(P, SYM, 1, px * 1.003, lv, f"SELFTEST-F-{tag}")
            if check("FULL: compra bracket 1 acc", "_error" not in r, r.get("_error", "")[:100]):
                f = wait_fill_or_cancel(P, r.get("id"), 60)
                if check("FULL: fill", float(f.get("filled_qty") or 0) >= 1, f"estado {f.get('status')}"):
                    o = P.req(f"/orders/{r['id']}?nested=true")
                    legs = [l for l in (o.get("legs") or []) if l.get("type") in ("stop", "stop_limit")]
                    if check("FULL: pata stop presente", bool(legs)):
                        leg = legs[0]; new = round_px(float(leg["stop_price"]) + 0.01)
                        body = {"stop_price": str(new)}
                        if leg.get("type") == "stop_limit":
                            body["limit_price"] = str(round_px(new * 0.995))
                        pr = P.req(f"/orders/{leg['id']}", "PATCH", body)
                        check("FULL: PATCH stop (conserva OCO)", "_error" not in pr and
                              abs(float(pr.get("stop_price") or 0) - new) < 1e-6, pr.get("_error", "")[:100] or f"→ {new}")
                    for oo in P.open_orders(SYM):
                        P.req(f"/orders/{oo['id']}", "DELETE")
                    time.sleep(2)
                    cr = P.req(f"/positions/{SYM}", "DELETE")
                    check("FULL: cierre posición", "_error" not in cr, cr.get("_error", "")[:100])

    # 4 · log en GitHub + 5 · Telegram
    resumen = {"ts": ahora.strftime("%Y-%m-%dT%H:%MZ"), "modo": MODE, "ok": len(ok),
               "fallos": fail, "avisos": warn}
    try:
        update_log(lambda pl: pl.__setitem__("selftest", resumen), f"selftest {MODE} [{ahora:%Y-%m-%dT%H:%M}Z]")
        check("GitHub log lectura+escritura", True)
    except Exception as e:
        check("GitHub log lectura+escritura", False, str(e)[:100])

    head = "🧪 SELFTEST " + MODE.upper() + (
        f" ❌ {len(fail)} FALLOS" if fail else (f" ✅ OK ({len(warn)} avisos)" if warn else " ✅ TODO OK"))
    tg("\n".join([head] + ["❌ " + x for x in fail] + ["⚠️ " + x for x in warn] + ["✅ " + x for x in ok]))
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        tg(f"🔴 selftest ERROR: {type(e).__name__}: {e}")
        raise
