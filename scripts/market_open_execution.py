#!/usr/bin/env python3
"""
Market Open Execution — GitHub Actions (v3, endurecido 2026-09-10)

Se ejecuta cada hora (13:35-19:35 UTC). Opera si han pasado entre open_window_min[0]
y open_window_min[1] minutos desde la apertura según el calendario de Alpaca (cubre
verano/invierno y retrasos del cron). Cada pasada vuelve a escanear y entra en lo nuevo;
los duplicados se evitan por posición existente, topes diarios y client_order_id único.

Cambios clave frente a v2:
  • Cuenta REAL solo con candidatos de la pipeline EW/Cowork (nunca del scanner
    de momentum), score ≥ 9.0, sin ETFs apalancados, y solo si el gate de
    rendimiento en paper está en verde (o forzado en risk_limits.json).
  • Sizing por riesgo: cada operación arriesga risk_per_trade_pct del equity.
  • Stop por ATR, TP = 2R mínimo. Entrada limit marcable (no market) con
    cancelación si no llena en 90 s.
  • Sin "perseguir" subidas: si el runup supera el umbral, se descarta.
  • Freno diario: si el día va por debajo de daily_loss_stop_pct, no entra.
  • Nunca se bloquea por el log: si GitHub falla, opera con el escaneo en vivo.
  • (2026-09-11) Fuentes extra (fuentes_extra.py): SEC Form 4, analistas, opciones, corto,
    sector, Reddit y macro FRED/Fear&Greed ajustan el score ±2 como máximo. Paper usa el
    ajuste completo; real solo la parte negativa. Si una fuente cae, ajuste 0 y se opera igual.
"""
import sys
from datetime import timedelta

import time

from common import (DRY_RUN, ET, FORCE_WINDOW, YF_ENABLED, atr14, compute_levels, count_new_entries_today,
                    day_pnl_pct, effective_limits, et_today, is_crypto, load_limits, log,
                    minutes_since_open, now_utc, paper_client, place_bracket_entry, read_log,
                    real_client, size_by_risk, tg, update_log, wait_fill_or_cancel)

ALLOW_KW = ("entrar", "largo", "comprar", "buy", "long", "open", "apertura", "premarket")
BLOCK_KW = ("no_entrar", "no entrar", "descartar", "evitar", "short", "vigilar_no")
ESTADOS = {"pendiente", "pendiente_reentrada", "pendiente_ew"}

# Universo del escaneo rápido de apertura (solo PAPER — fuente live_open_scan)
UNIVERSE_QUICK = ["NVDA", "AMD", "MSFT", "AAPL", "GOOGL", "META", "AMZN", "CRM", "DELL", "CRWD",
                  "PLTR", "COIN", "SMCI", "ARM", "NFLX", "TSLA", "AVGO", "MU", "AMAT", "XLK", "SMH",
                  "TQQQ", "SOXL"]


def es_entrada(a):
    a = (a or "").lower()
    return not any(b in a for b in BLOCK_KW) and any(k in a for k in ALLOW_KW)


def expira_ok(c, ahora):
    exp = c.get("expira") or ""
    if not exp:
        return True
    try:
        from datetime import datetime
        return datetime.fromisoformat(exp.replace("Z", "+00:00")) > ahora
    except Exception:
        return True


def macro_block(plog, now_et):
    """Regla del log (lección 29): en días de macro_context.dias_riesgo_macro no se entra antes de las 10:30 ET."""
    pa = plog.get("parametros_activos") or {}
    if not pa.get("bloqueo_entradas_dias_riesgo_macro"):
        return None
    hoy = now_et.date().isoformat()
    ev = [str(d) for d in ((plog.get("macro_context") or {}).get("dias_riesgo_macro") or []) if str(d).startswith(hoy)]
    if ev and (now_et.hour, now_et.minute) < (10, 30):
        return ev[0]
    return None


def fuente(c):
    return c.get("fuente") or c.get("source") or c.get("fuente_señal") or "?"


def _base(c):
    return float(c.get("score_ajustado", c.get("score", 0)) or 0)


def aplicar_fuentes_extra(uniq, min_paper):
    """Enriquece los candidatos con scripts/fuentes_extra.py (SEC, analistas, opciones, sector,
    social, macro) y fija en cada uno _score_base/_score_paper/_score_real/_ajuste/_partes.
    Paper usa el ajuste completo; real solo la parte negativa. NUNCA lanza: si algo falla,
    todos los scores quedan como el base (ajuste 0) y se opera igual."""
    for c in uniq:
        b = _base(c)
        c.update(_score_base=b, _score_paper=b, _score_real=b, _ajuste=0.0, _partes={})
    try:
        import fuentes_extra as fx
        if not fx.ACTIVADA:
            return None, {}
        macro = fx.macro_extra()
        objetivo = [c["ticker"] for c in uniq
                    if c["_score_base"] >= min_paper - fx.AJUSTE_MAX and "/" not in c["ticker"]][:fx.MAX_TICKERS]
        t0 = time.time()
        datos = fx.enriquecer(objetivo)
        for c in uniq:
            aj, partes = fx.ajuste(datos.get(c["ticker"]), macro)
            sp, sr = fx.scores_por_cuenta(c["_score_base"], aj)
            c.update(_score_paper=sp, _score_real=sr, _ajuste=aj, _partes=partes)
        log(f"Fuentes extra: macro {macro.get('regimen')} ({macro.get('ajuste'):+.2f}) · "
            f"{len(datos)}/{len(objetivo)} enriquecidos en {time.time() - t0:.0f}s")
        return macro, fx.resumen_estado()
    except Exception as e:  # noqa: BLE001 — las fuentes extra nunca bloquean la ejecución
        log(f"⚠️ fuentes extra no disponibles ({type(e).__name__}: {e}); se opera con el score base")
        for c in uniq:
            c.update(_score_paper=c["_score_base"], _score_real=c["_score_base"], _ajuste=0.0, _partes={})
        return None, {}


def live_scan(ahora):
    """Momentum de apertura (paper). Devuelve candidatos con fuente live_open_scan."""
    out = []
    if not YF_ENABLED:
        return out
    try:
        import yfinance as yf
    except Exception:
        return out
    t0 = time.time()
    for t in UNIVERSE_QUICK:
        if time.time() - t0 > 120:          # presupuesto: nunca bloquear la apertura
            log("  live_scan: presupuesto de 120 s agotado"); break
        try:
            hist = yf.Ticker(t).history(period="3d", interval="5m", timeout=10)
            hd = yf.Ticker(t).history(period="5d", timeout=10)
            if len(hist) < 10 or len(hd) < 2:
                continue
            price = float(hist["Close"].iloc[-1]); prev = float(hd["Close"].iloc[-2])
            ret1d = (price / prev - 1) * 100
            vol = hist["Volume"]; vr = float(vol.iloc[-3:].mean()) / float(vol.mean() or 1)
            hi52 = float(yf.Ticker(t).history(period="1y", timeout=10)["High"].max())
            s = 0.0
            s += 3 if ret1d > 2 else 1.5 if ret1d > 0.5 else 0
            s += 2.5 if vr > 2.5 else 1.2 if vr > 1.5 else 0
            s += 1.5 if (hi52 - price) / hi52 * 100 < 5 else 0
            if s >= 4.0:
                norm = round(min(s / 8 * 10, 10), 2)
                out.append({"ticker": t, "symbol": t, "estado": "pendiente", "score": norm,
                            "score_ajustado": norm, "accion_recomendada": "entrar_apertura",
                            "precio_referencia": round(prev, 2), "stop_pct": 0.05,
                            "expira": (ahora + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "fuente": "live_open_scan", "notas": f"live 1d={ret1d:+.1f}% vol={vr:.1f}x"})
        except Exception as e:
            log(f"  live_scan {t}: {e}")
    return sorted(out, key=lambda x: x["score"], reverse=True)


def main():
    ahora = now_utc()
    hoy = et_today()
    log(f"=== market-open-execution v3 {ahora:%Y-%m-%d %H:%M} UTC {'[DRY_RUN]' if DRY_RUN else ''} ===")
    P, R = paper_client(), real_client()
    limits = load_limits()
    ex = limits["execution"]

    clock = P.clock()
    if not clock.get("is_open") and not FORCE_WINDOW:
        log("Mercado cerrado — nada que hacer."); return
    mso = minutes_since_open(P)
    w0, w1 = ex["open_window_min"]
    if not FORCE_WINDOW and (mso is None or mso < w0 or mso > w1):
        log(f"market-open: fuera de ventana ({'?' if mso is None else f'{mso:.0f}'} min desde apertura; "
            f"ventana {w0}-{w1}). Sin órdenes.")
        return
    # Se ejecuta cada hora durante la sesión. Sin bloqueo "ya ejecutado hoy": los duplicados
    # los evitan "ya en cartera", los topes diarios y el client_order_id único por día y ticker.
    try:
        plog, _ = read_log()
    except Exception as e:           # si GitHub falla, se opera igual con el escaneo en vivo (paper)
        log(f"⚠️ no se pudo leer el log ({e}); se continúa solo con escaneo en vivo")
        plog = {}
    primera_hoy = (plog.get("ejecucion_apertura") or {}).get("fecha") != hoy
    evento = None if FORCE_WINDOW else macro_block(plog, ahora.astimezone(ET))
    if evento:
        msg = f"⏸️ market-open: día de riesgo macro ({evento}). Sin entradas hasta las 10:30 ET; la siguiente pasada horaria operará."
        log(msg); tg(msg); return

    L = effective_limits(plog, limits)
    lp, lr = L["paper"], L["real"]
    log(f"Paper: score≥{lp['min_score']} maxpos={lp['max_positions']} | "
        f"Real: {'ACTIVA' if lr['active'] else 'OFF'} ({lr['why']}) score≥{lr['min_score']}")

    # ── candidatos ──
    cands = [c for c in plog.get("candidatos_validados", [])
             if c.get("estado") in ESTADOS and expira_ok(c, ahora) and es_entrada(c.get("accion_recomendada"))]
    cands += live_scan(ahora)[:4]
    seen, uniq = set(), []
    for c in sorted(cands, key=lambda x: float(x.get("score_ajustado", x.get("score", 0)) or 0), reverse=True):
        t = (c.get("ticker") or c.get("symbol") or "").upper()
        if t and t not in seen:
            seen.add(t); c["ticker"] = t; uniq.append(c)
    log(f"Candidatos: {[(c['ticker'], c.get('score_ajustado', c.get('score')), fuente(c)) for c in uniq]}")
    macro_x, estado_x = aplicar_fuentes_extra(uniq, lp["min_score"])
    uniq.sort(key=lambda c: c["_score_paper"], reverse=True)

    # ── estado cuentas ──
    acct = P.account(); eq = float(acct["equity"]); bp = float(acct["buying_power"])
    pos = {p["symbol"] for p in P.positions() if not is_crypto(p)}
    n_open = len(pos)
    n_new = count_new_entries_today(P, "GHA-")
    paper_dd = day_pnl_pct(acct)

    if R:
        acct_r = R.account(); eq_r = float(acct_r.get("equity", 0)); bp_r = float(acct_r.get("buying_power", 0))
        pos_r = {p["symbol"] for p in R.positions() if not is_crypto(p)}
        n_new_r = count_new_entries_today(R, "GHA-REAL-")
        real_dd = day_pnl_pct(acct_r)
    else:
        eq_r = bp_r = 0; pos_r = set(); n_new_r = 0; real_dd = 0
        lr["active"], lr["why"] = False, "sin credenciales reales"

    ejecutados, descartados = [], []
    tag = ahora.strftime("%Y%m%d")

    for c in uniq:
        t = c["ticker"]; src = fuente(c)
        base, sc, sc_real = c["_score_base"], c["_score_paper"], c["_score_real"]
        log(f"\n── {t} score {sc:.2f} (base {base:.2f}, fuentes extra {c['_ajuste']:+.2f} {c['_partes']}) [{src}]")
        if sc < lp["min_score"]:
            descartados.append((t, f"score {sc:.1f}<{lp['min_score']}")); continue
        price = P.latest_price(t)
        if not price:
            descartados.append((t, "sin precio")); continue
        ref = float(c.get("precio_referencia") or 0)
        if ref > 0:
            move = (price / ref - 1) * 100
            thr = ex["max_runup_pct_high_score"] if base >= ex["high_score"] else \
                min(float(c.get("runup_tolerance_pct", ex["max_runup_pct"])), ex["max_runup_pct"])
            if move > thr:
                descartados.append((t, f"runup {move:+.1f}%>{thr:.0f}%")); continue
        lv = compute_levels(price, float(c["stop_pct"]) if c.get("stop_pct") else None, atr14(t, P), ex)
        limit_px = price * (1 + ex["entry_limit_slippage_pct"] / 100)
        rec = {"ticker": t, "score": sc, "score_base": base, "ajuste": c["_ajuste"], "partes": c["_partes"],
               "fuente": src, "precio": round(price, 2), **{k: lv[k] for k in ("stop", "tp", "stop_pct", "rr")}}

        # PAPER
        if t in pos:
            descartados.append((t, "ya en cartera paper"))
        elif n_open >= lp["max_positions"]:
            descartados.append((t, f"max posiciones paper {lp['max_positions']}"))
        elif n_new >= lp["max_new_per_day"]:
            descartados.append((t, "max entradas/día paper"))
        elif paper_dd <= -lp["daily_loss_stop_pct"]:
            descartados.append((t, f"freno diario paper {paper_dd:.1f}%"))
        else:
            q = size_by_risk(eq, limit_px, limit_px - lv["stop"], lp["risk_per_trade_pct"], lp["max_position_pct"], bp)
            if q < 1:
                descartados.append((t, "qty 0 paper"))
            else:
                r = place_bracket_entry(P, t, q, limit_px, lv, f"GHA-{tag}-{t}")
                if "_error" in r:
                    descartados.append((t, "paper: " + r["_error"][:80]))
                else:
                    f = wait_fill_or_cancel(P, r.get("id"), ex["entry_fill_timeout_s"])
                    fq = float(f.get("filled_qty") or 0)
                    if fq > 0 or DRY_RUN:
                        rec.update(qty=int(fq) if fq else q, oid=r.get("id"),
                                   fill=float(f.get("filled_avg_price") or limit_px))
                        ejecutados.append(rec); n_open += 1; n_new += 1; pos.add(t)
                    else:
                        descartados.append((t, "paper: no llenó (cancelada)"))

        # REAL
        if not lr["active"]:
            continue
        why = None
        if sc_real < lr["min_score"]: why = f"score real {sc_real:.2f}<{lr['min_score']}"
        elif src in lr["blocked_sources"]: why = f"fuente {src} bloqueada en real"
        elif t in L["leveraged"] and not lr["allow_leveraged_etf"]: why = "ETF apalancado"
        elif t in pos_r: why = "ya en cartera real"
        elif len(pos_r) >= lr["max_positions"]: why = "max posiciones real"
        elif n_new_r >= lr["max_new_per_day"]: why = "max entradas/día real"
        elif real_dd <= -lr["daily_loss_stop_pct"]: why = f"freno diario real {real_dd:.1f}%"
        if why:
            log(f"  REAL no: {why}"); continue
        qr = size_by_risk(eq_r, limit_px, limit_px - lv["stop"], lr["risk_per_trade_pct"], lr["max_position_pct"], bp_r)
        if qr < 1:
            log("  REAL no: equity insuficiente para el riesgo definido"); continue
        rr_ = place_bracket_entry(R, t, qr, limit_px, lv, f"GHA-REAL-{tag}-{t}")
        if "_error" in rr_:
            log(f"  REAL error: {rr_['_error'][:120]}"); continue
        fr = wait_fill_or_cancel(R, rr_.get("id"), ex["entry_fill_timeout_s"])
        fqr = float(fr.get("filled_qty") or 0)
        if fqr > 0 or DRY_RUN:
            rec["real_qty"] = int(fqr) if fqr else qr
            if rec not in ejecutados:
                ejecutados.append(rec)
            pos_r.add(t); n_new_r += 1

    # ── log ──
    def mutate(pl):
        for e in ejecutados:
            for c in pl.get("candidatos_validados", []):
                if (c.get("ticker") or c.get("symbol") or "").upper() == e["ticker"] and c.get("estado") in ESTADOS:
                    c["estado"] = "ejecutado"; c["fecha_ejecucion"] = ahora.strftime("%Y-%m-%d %H:%M")
            op_id = f"GHA-{tag}-{e['ticker']}"
            if not any(o.get("id") == op_id for o in pl.get("operaciones", [])):
                pl.setdefault("operaciones", []).append({
                    "id": op_id, "simbolo": e["ticker"], "tipo": "GH_Actions_open", "fuente": e["fuente"],
                    "score_entrada": e["score"], "score_base": e.get("score_base"),
                    "ajuste_fuentes": e.get("ajuste"), "fuentes_extra": e.get("partes"), "fecha_entrada": hoy, "precio_entrada": e.get("fill", e["precio"]),
                    "precio_stop": e["stop"], "precio_tp": e["tp"], "stop_pct": e["stop_pct"], "rr": e["rr"],
                    "qty": e.get("qty", 0), "real_qty": e.get("real_qty", 0), "estado": "abierta",
                    "orden_id": e.get("oid"), "cuenta": "paper+real" if e.get("real_qty") else "paper"})
        pl["ejecucion_apertura"] = {"fecha": hoy, "hora_utc": ahora.strftime("%H:%M"),
                                    "min_desde_apertura": round(mso or 0, 1),
                                    "real_activa": lr["active"], "real_motivo": lr["why"],
                                    "ejecutados": [e["ticker"] for e in ejecutados],
                                    "descartados": [f"{t}: {w}" for t, w in descartados][:20],
                                    "ajustes_fuentes": {c["ticker"]: {"base": c["_score_base"], "paper": c["_score_paper"],
                                                                      "real": c["_score_real"], "partes": c["_partes"]}
                                                        for c in uniq[:12] if c.get("_partes")}}
        if macro_x is not None or estado_x:
            pl["fuentes_extra"] = {"_doc": "scripts/fuentes_extra.py: SEC Form 4, analistas, opciones, corto, sector, "
                                           "Reddit (ApeWisdom), FRED y Fear&Greed. Ajuste acotado ±2; paper usa el "
                                           "ajuste completo, real solo la parte negativa. Una fuente caída = ajuste 0.",
                                   "ts": ahora.strftime("%Y-%m-%dT%H:%MZ"), "macro": macro_x, "estado": estado_x}

    try:
        update_log(mutate, f"market-open v3 [{ahora:%Y-%m-%dT%H:%M}Z] {len(ejecutados)} ejecutadas")
    except Exception as e:           # las órdenes ya están en Alpaca; un fallo del log no debe ocultarlas
        log(f"⚠️ no se pudo escribir el log: {e}")
        descartados.append(("LOG", f"no escrito: {str(e)[:60]}"))
    if not ejecutados and not primera_hoy:
        log("Sin ejecuciones en esta pasada (no se envía Telegram)."); return

    lines = [f"🚀 APERTURA {ahora:%d/%m %H:%M}UTC (+{mso or 0:.0f}min) | Real: {'ON' if lr['active'] else 'OFF'}"]
    for e in ejecutados:
        lines.append(f"✅ {e['ticker']} {e.get('qty', 0)}acc @ {e.get('fill', e['precio']):.2f} | SL {e['stop']} "
                     f"({e['stop_pct']}%) TP {e['tp']} | s={e['score']:.1f}"
                     f"{(' (base ' + format(e['score_base'], '.1f') + ')') if e.get('ajuste') else ''} {('+REAL ' + str(e['real_qty'])) if e.get('real_qty') else ''}")
    if not ejecutados:
        lines.append("Sin ejecuciones.")
    if descartados:
        lines.append("Descartados: " + "; ".join(f"{t} ({w})" for t, w in descartados[:6]))
    if not lr["active"]:
        lines.append(f"ℹ️ Real OFF: {lr['why']}")
    tg("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"🔴 market-open ERROR: {type(e).__name__}: {e}")
        raise
