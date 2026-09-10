#!/usr/bin/env python3
"""
Intraday Monitor — GitHub Actions (v3, endurecido 2026-09-10). Cada 30 min en sesión.

Gestión de riesgo basada en R (R = entrada − stop inicial), paper y real:
  1. Posición SIN stop  → coloca stop GTC de protección (naked_stop_pct).
  2. Pérdida ≥ emergency_loss_pct (gap o stop saltado) → cierre inmediato.
  3. Beneficio ≥ breakeven_at_r·R → sube el stop a break-even.
  4. Beneficio ≥ lock_at_r·R      → sube el stop a +lock_r·R.
     Los stops se MODIFICAN (PATCH) conservando el OCO del bracket, nunca se
     cancelan y recrean (v2 perdía el take-profit al hacerlo).
  5. Cancela entradas GHA sin llenar con más de stale_entry_min minutos.

Eliminado de v2: la venta parcial al +7% (fallaba siempre porque las acciones
están retenidas por las patas del bracket) y el break-even al +5% fijo, que
recortaba ganadoras antes del TP (evidencia: ganancia media $129 vs pérdida
media $241 en 133 cierres paper).
Crypto y restos < $5: se ignoran (los gestionan las tareas crypto).
"""
from datetime import datetime, timezone

from common import (DRY_RUN, load_limits, log, now_utc, paper_client, read_log, real_client,
                    round_px, tg, update_log)


def stop_orders_by_symbol(orders):
    d = {}
    for o in orders:
        if o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit", "trailing_stop"):
            d.setdefault(o["symbol"], []).append(o)
    return d


def initial_risk(sym, entry, cur_stop, plog):
    """R por acción: del log (precio_stop de la operación abierta) o del stop actual si está por debajo."""
    for op in reversed(plog.get("operaciones", [])):
        if (op.get("simbolo") or op.get("symbol")) == sym and str(op.get("estado", "")).startswith("abiert"):
            s = op.get("precio_stop") or op.get("stop_loss")
            try:
                s = float(s)
                if 0 < s < entry:
                    return entry - s
            except (TypeError, ValueError):
                pass
    if cur_stop and cur_stop < entry:
        return entry - cur_stop
    return entry * 0.05


def manage(alp, plog, M, acciones):
    if alp is None:
        return
    positions = alp.positions()
    orders = alp.open_orders()
    stops = stop_orders_by_symbol(orders)

    for p in positions:
        sym = p["symbol"]; qty = float(p["qty"]); side = p.get("side", "long")
        price = float(p.get("current_price") or 0); entry = float(p.get("avg_entry_price") or 0)
        pnl_pct = float(p.get("unrealized_plpc") or 0) * 100
        crypto = p.get("asset_class") == "crypto"
        if side != "long" or qty <= 0 or price <= 0 or crypto:
            continue  # crypto: la gestionan las tareas crypto (volatilidad distinta)
        if abs(float(p.get("market_value") or 0)) < 5:
            continue  # restos ('dust') sin importancia
        tag = f"[{alp.name}] {sym}"

        # 2 · emergencia
        if pnl_pct <= -M["emergency_loss_pct"]:
            for o in [o for o in orders if o.get("symbol") == sym]:
                alp.req(f"/orders/{o['id']}", "DELETE")
            r = alp.req(f"/positions/{sym.replace('/', '')}", "DELETE")
            acciones.append(f"🚨 {tag}: cierre emergencia {pnl_pct:.1f}% {'(error ' + r['_error'][:60] + ')' if '_error' in r else ''}")
            continue

        my_stops = stops.get(sym, [])
        # 1 · posición desnuda
        if not my_stops:
            sp = round_px(max(entry, price) * (1 - M["naked_stop_pct"] / 100))
            if sp >= price:
                sp = round_px(price * 0.99)
            avail = float(p.get("qty_available") or qty)
            if avail >= 1:
                r = alp.req("/orders", "POST", {"symbol": sym, "qty": str(int(avail)), "side": "sell",
                                                "type": "stop", "stop_price": str(sp), "time_in_force": "gtc",
                                                "client_order_id": f"GHA-PROTECT-{now_utc():%Y%m%d%H%M}-{sym}"[:48]})
                acciones.append(f"🛡️ {tag}: sin stop → stop protección {sp} "
                                f"{'OK' if '_error' not in r else 'ERROR ' + r['_error'][:80]}")
            else:
                acciones.append(f"⚠️ {tag}: sin stop y acciones retenidas por otra orden — revisar a mano")
            continue

        # 3/4 · subir stop por R
        so = max(my_stops, key=lambda o: float(o.get("stop_price") or 0))
        cur = float(so.get("stop_price") or 0)
        R = initial_risk(sym, entry, cur, plog)
        gain_r = (price - entry) / R if R > 0 else 0
        target = None
        if gain_r >= M["lock_at_r"]:
            target = entry + M["lock_r"] * R; why = f"+{gain_r:.1f}R → lock +{M['lock_r']}R"
        elif gain_r >= M["breakeven_at_r"]:
            target = entry * 1.001; why = f"+{gain_r:.1f}R → break-even"
        if target is None:
            continue
        target = round_px(target)
        if target <= cur * 1.002 or target >= price * 0.995:
            continue
        body = {"stop_price": str(target)}
        if so.get("type") == "stop_limit":
            body["limit_price"] = str(round_px(target * 0.995))
        r = alp.req(f"/orders/{so['id']}", "PATCH", body)
        if "_error" in r:
            acciones.append(f"⚠️ {tag}: no se pudo subir stop {cur}→{target}: {r['_error'][:80]}")
        else:
            acciones.append(f"📈 {tag}: stop {cur} → {target} ({why})")

    # 5 · entradas GHA colgadas
    stale = load_limits()["management"]["stale_entry_min"]
    for o in orders:
        cid = o.get("client_order_id") or ""
        if o.get("side") == "buy" and cid.startswith("GHA-") and o.get("status") in ("new", "accepted", "partially_filled"):
            try:
                age = (now_utc() - datetime.fromisoformat(o["submitted_at"].replace("Z", "+00:00"))).total_seconds() / 60
            except Exception:
                continue
            if age > stale:
                alp.req(f"/orders/{o['id']}", "DELETE")
                acciones.append(f"🧹 [{alp.name}] cancelada entrada colgada {o['symbol']} ({age:.0f} min)")


def main():
    ahora = now_utc()
    log(f"=== intraday-monitor v3 {ahora:%Y-%m-%d %H:%M} UTC {'[DRY_RUN]' if DRY_RUN else ''} ===")
    P, R = paper_client(), real_client()
    if not P.clock().get("is_open"):
        log("Mercado cerrado."); return
    M = load_limits()["management"]
    plog, _ = read_log()
    acciones = []
    manage(P, plog, M, acciones)
    manage(R, plog, M, acciones)

    if not acciones:
        log("Sin acciones."); return
    for a in acciones:
        log("  " + a)

    def mutate(pl):
        lst = pl.setdefault("intraday_monitor_log", [])
        if isinstance(lst, list):
            lst.append({"ts": ahora.strftime("%Y-%m-%dT%H:%MZ"), "fuente": "gha-v3", "acciones": acciones})
            del lst[:-200]
    update_log(mutate, f"intraday-monitor v3 [{ahora:%Y-%m-%dT%H:%M}Z] {len(acciones)} acciones")
    tg(f"📊 INTRADAY {ahora:%H:%M}UTC\n" + "\n".join(acciones))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"🔴 intraday-monitor ERROR: {type(e).__name__}: {e}")
        raise
