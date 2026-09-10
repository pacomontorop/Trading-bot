#!/usr/bin/env python3
"""
Pre-Close Manager — GitHub Actions (v1, 2026-09-10). Réplica autónoma de la tarea Cowork
"pre-close-manager": nunca mantener una posición a través de unos resultados.

Se ejecuta varias veces por la tarde; solo actúa entre 5 y 30 min antes del cierre
(calendario Alpaca: cubre horario de verano/invierno y cierres anticipados).
Para cada posición de acciones (paper y real) consulta EarningsWhispers:
  • resultados HOY después del cierre (AMC)              → cerrar
  • resultados el PRÓXIMO día de mercado antes de abrir  → cerrar
  • resultados hoy sin hora conocida                    → cerrar (conservador)
Cuenta real: si cerrar supondría el 4º day-trade en 5 días (regla PDT, < $25k), no
cierra y avisa por Telegram para decisión manual.
Desactivable con parametros_activos.no_holdings_earnings_overnight.regla vacía / false.
"""
from datetime import date, datetime, timedelta, timezone

from common import (DRY_RUN, FORCE_WINDOW, ET, et_today, is_crypto, log, now_utc, paper_client, read_log,
                    real_client, session_today, tg, update_log)
from datasources import ew_stock


def next_session(alp, today: date):
    cal = alp.req(f"/calendar?start={(today + timedelta(days=1)).isoformat()}&end={(today + timedelta(days=10)).isoformat()}")
    return date.fromisoformat(cal[0]["date"]) if isinstance(cal, list) and cal else None


def should_close(info: dict, today: date, nxt: date):
    if not info or not info.get("nextEPSDate"):
        return False, ""
    d = date.fromisoformat(info["nextEPSDate"][:10])
    rt = info.get("releaseTime")
    if d == today and rt == 3:
        return True, "resultados hoy AMC"
    if d == today and rt not in (1, 3):
        return True, "resultados hoy (hora desconocida)"
    if nxt and d == nxt and rt in (1, None):
        return True, f"resultados {d:%d/%m} BMO"
    return False, ""


def main():
    ahora = now_utc()
    P, R = paper_client(), real_client()
    s = session_today(P)
    if not s:
        log("Hoy no hay sesión."); return
    mtc = (s[1].astimezone(timezone.utc) - ahora).total_seconds() / 60
    if not FORCE_WINDOW and not (5 <= mtc <= 30):
        log(f"Fuera de ventana pre-cierre ({mtc:.0f} min al cierre)."); return
    plog, _ = read_log()
    regla = ((plog.get("parametros_activos") or {}).get("no_holdings_earnings_overnight"))
    if regla is False or (isinstance(regla, dict) and regla.get("activo") is False):
        log("Regla no_holdings_earnings_overnight desactivada."); return

    today = date.fromisoformat(et_today()); nxt = next_session(P, today)
    acciones, cache = [], {}
    for alp in (P, R):
        if alp is None:
            continue
        acct = alp.account()
        for p in alp.positions():
            if is_crypto(p) or abs(float(p.get("market_value") or 0)) < 5:
                continue
            sym = p["symbol"]
            info = cache.setdefault(sym, ew_stock(sym))
            close, why = should_close(info, today, nxt)
            if not close:
                continue
            tag = f"[{alp.name}] {sym}"
            if alp.name == "REAL" and float(acct.get("equity", 0)) < 25000:
                opened_today = any(o.get("symbol") == sym and o.get("side") == "buy" and float(o.get("filled_qty") or 0) > 0
                                   and (o.get("filled_at") or "")[:10] == ahora.strftime("%Y-%m-%d")
                                   for o in alp.orders_since(s[0].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
                if opened_today and int(acct.get("daytrade_count") or 0) >= 3:
                    acciones.append(f"⚠️ {tag}: {why} pero cerrar sería el 4º day-trade (PDT). REVISAR A MANO.")
                    continue
            for o in alp.open_orders(sym):
                alp.req(f"/orders/{o['id']}", "DELETE")
            r = alp.req(f"/positions/{sym}", "DELETE")
            pnl = float(p.get("unrealized_pl") or 0)
            acciones.append(f"🔒 {tag}: cerrada antes de {why} · P&L ${pnl:+.2f}"
                            + (f" (error {r['_error'][:60]})" if "_error" in r else ""))

    log(f"pre-close {ahora:%H:%M}Z · {mtc:.0f} min al cierre · {len(acciones)} acciones")
    if not acciones:
        return
    for a in acciones:
        log("  " + a)

    def mutate(pl):
        lst = pl.setdefault("pre_close_log", [])
        if isinstance(lst, list):
            lst.append({"ts": ahora.strftime("%Y-%m-%dT%H:%MZ"), "fuente": "gha", "acciones": acciones})
            del lst[:-200]
    update_log(mutate, f"pre-close gha [{ahora:%Y-%m-%dT%H:%M}Z] {len(acciones)} cierres")
    tg(f"🔒 PRE-CIERRE {ahora:%H:%M}UTC\n" + "\n".join(acciones))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"🔴 pre-close ERROR: {type(e).__name__}: {e}")
        raise
