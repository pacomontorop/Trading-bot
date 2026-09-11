#!/usr/bin/env python3
"""
EW Pipeline — GitHub Actions (v1, 2026-09-10). Genera candidatos pre-earnings de forma
autónoma, sin depender del PC ni de Cowork, cruzando varias fuentes (datasources.py):

  EarningsWhispers  calendario + whisper vs consenso
  Nasdaq            calendario de respaldo
  Yahoo             beat rate y reacción histórica (8 trimestres), revisiones 30 d
  Alpaca            precio, momentum 5 d, noticias 3 d, tendencia SPY
  VIX               régimen de mercado

Timing (estrategia pre-drift + cuenta real < $25k → evitar day-trades):
  E = próxima sesión (día de ENTRADA, en la apertura)
  X = sesión siguiente a E (día de SALIDA, antes del cierre; lo hace pre_close.py)
  Eventos válidos: resultados AMC el día X, o BMO/sin hora el día siguiente a X.
  Así siempre se mantiene al menos una noche y nunca se atraviesa el resultado.

Salida: candidatos_validados con fuente "ew_pipeline_gha", estado "pendiente_ew",
accion "entrar_apertura" (los ejecuta market_open_execution.py con todos los límites).
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

from common import DRY_RUN, et_today, log, now_utc, paper_client, tg, update_log
from datasources import (alpaca_daily_bars, alpaca_news_count, alpaca_snapshots, earnings_calendar,
                         ew_results_today, ew_stock, health_check, macro_state, yf_earnings_profile)

FUENTE = "ew_pipeline_gha"
PEND = ("pendiente", "pendiente_ew", "pendiente_reentrada")
MAX_EVAL = 40


def sessions(alp, start: date, n_days: int = 14) -> list:
    cal = alp.req(f"/calendar?start={start.isoformat()}&end={(start + timedelta(days=n_days)).isoformat()}")
    return [date.fromisoformat(c["date"]) for c in cal] if isinstance(cal, list) else []


def entry_exit_days(alp, mode: str = "next"):
    """E = día de entrada, X = día de salida (sesión siguiente a E), X1 = sesión siguiente a X.
    mode "today": E = hoy si hoy hay sesión (se usa dentro del workflow de apertura).
    mode "next":  E = próxima sesión que aún no ha empezado (ejecución nocturna/previa)."""
    today = date.fromisoformat(et_today())
    ss = sessions(alp, today)
    if not ss:
        return None
    if ss[0] == today and mode != "today":
        clock = alp.clock()
        if clock.get("is_open") or not str(clock.get("next_open", "")).startswith(today.isoformat()):
            ss = ss[1:]                  # la sesión de hoy ya empezó o terminó
    if len(ss) < 3:
        return None
    return ss[0], ss[1], ss[2]


def score_candidate(f: dict, macro: dict):
    """Puntuación 0-10 con desglose. f: asym_pct, beat_rate, n_q, reaction_pct, rev_up, rev_down, mom5, news."""
    p = {}
    a = f.get("asym_pct")
    if a is not None:
        p["whisper"] = round(max(0.0, min(a, 15.0)) / 15.0 * 2.0 if a >= 0 else max(-1.0, a / 10.0), 2)
    br, n = f.get("beat_rate"), f.get("n_q") or 0
    if br is not None and n >= 4:
        p["beat_rate"] = 3.0 if br >= 87.5 else 2.0 if br >= 75 else 1.0 if br >= 62.5 else -1.0 if br < 50 else 0.0
    r = f.get("reaction_pct")
    if r is not None:
        p["reaccion"] = round(min(r / 4.0, 1.0) * 1.5, 2) if r > 0 else (-0.5 if r < -2 else 0.0)
    up, dn = f.get("rev_up"), f.get("rev_down")
    if up is not None and dn is not None:
        net = up - dn
        p["revisiones"] = 1.5 if net >= 3 else 0.75 if net >= 1 else -1.0 if net <= -2 else 0.0
    m = f.get("mom5")
    if m is not None:
        p["momentum5d"] = 1.0 if -6 <= m <= 3 else -1.5 if m > 8 else 0.0
    vix, spy_up = macro.get("vix"), macro.get("spy_above_ma50")
    mac = 0.0
    if vix is not None and vix > 28:
        mac -= 2.0
    elif (vix is None or vix < 20) and spy_up:
        mac += 1.0
    if spy_up is False:
        mac -= 0.5
    p["macro"] = mac
    if (f.get("news") or 0) >= 3:
        p["noticias"] = 0.5
    total = max(0.0, min(10.0, sum(p.values())))
    return round(total, 2), p


def main():
    ahora = now_utc()
    log(f"=== ew-pipeline {ahora:%Y-%m-%d %H:%M} UTC {'[DRY_RUN]' if DRY_RUN else ''} ===")
    P = paper_client()
    salud = health_check()
    for k, (ok, det) in salud.items():
        log(f"  fuente {k}: {'OK' if ok else 'FALLO'} ({det})")

    days = entry_exit_days(P, os.environ.get("EW_MODE", "next"))
    if not days:
        log("Sin calendario de sesiones."); return
    E, X, X1 = days
    eventos = [(r, X, "AMC") for r in earnings_calendar(X) if r.get("releaseTime") == 3]
    eventos += [(r, X1, "BMO" if r.get("releaseTime") == 1 else "desconocido") for r in earnings_calendar(X1)
                if r.get("releaseTime") in (1, None)]
    tick = []
    for r, d, tm in eventos:
        t = (r.get("ticker") or "").upper()
        if t and t.isalpha() and len(t) <= 5 and t not in {x[0] for x in tick}:
            tick.append((t, d, tm, int(r.get("total") or 0)))
    tick.sort(key=lambda x: -x[3])
    log(f"Entrada {E} · salida {X} · eventos {len(tick)} (AMC {X} + BMO {X1})")

    snaps = alpaca_snapshots([t for t, *_ in tick[:120]]) if tick else {}
    macro = macro_state()
    cands, evaluados = [], []
    for t, d, tm, nan in tick:
        if len(evaluados) >= MAX_EVAL:
            break
        s = snaps.get(t) or {}
        price = ((s.get("dailyBar") or {}).get("c")) or ((s.get("latestTrade") or {}).get("p"))
        if not price or price < 5 or (nan and nan < 3):
            continue
        ew = ew_stock(t) or {}
        cons, wh = ew.get("consensusEst"), ew.get("whisper")
        asym = ((wh - cons) / abs(cons) * 100) if (wh is not None and cons not in (None, 0)) else None
        prof = yf_earnings_profile(t)
        bars = alpaca_daily_bars(t, 12)
        mom5 = ((bars[-1]["c"] / bars[-6]["c"] - 1) * 100) if len(bars) >= 6 else None
        f = {"asym_pct": asym, "beat_rate": prof.get("beat_rate"), "n_q": prof.get("n"),
             "reaction_pct": prof.get("avg_reaction_pct"), "rev_up": prof.get("rev_up_30d"),
             "rev_down": prof.get("rev_down_30d"), "mom5": mom5, "news": alpaca_news_count(t)}
        sc, parts = score_candidate(f, macro)
        # referencia para el filtro de runup: cierre anterior a la sesión de entrada
        ref = ((s.get("prevDailyBar") or {}).get("c")) if os.environ.get("EW_MODE") == "today" else None
        ref = ref or price
        rec = {"ticker": t, "symbol": t, "estado": "pendiente_ew", "score": sc, "score_ajustado": sc,
               "score_breakdown": parts, "accion_recomendada": "entrar_apertura", "fuente": FUENTE,
               "precio_referencia": round(float(ref), 2), "runup_tolerance_pct": 5.0,
               "expira": f"{E.isoformat()}T20:00:00Z", "fecha_entrada_prevista": E.isoformat(),
               "cerrar_antes": X.isoformat(), "earnings_fecha": d.isoformat(), "earnings_timing": tm,
               "consenso_eps": cons, "whisper": wh, "asimetria_pct": round(asym, 2) if asym is not None else None,
               "beat_rate": prof.get("beat_rate"), "reaccion_media_pct": prof.get("avg_reaction_pct"),
               "momentum_5d_pct": round(mom5, 2) if mom5 is not None else None, "analistas": nan,
               "fecha_scan": ahora.strftime("%Y-%m-%dT%H:%M:%SZ")}
        evaluados.append(rec)
        log(f"  {t:6} {tm:11} score {sc:5.2f} {parts}")
    cands = sorted(evaluados, key=lambda c: -c["score"])

    # ── ew_cache: datos EW para las tareas que no pueden llamar a la API (sin shell / sin Referer) ──
    cal_cache, tick_cache, res_cache = {}, {}, []
    try:
        for dd in sessions(P, E)[:5]:
            rows = earnings_calendar(dd)
            cal_cache[dd.isoformat()] = [{k: r.get(k) for k in ("ticker", "company", "releaseTime", "total", "fuente")}
                                         for r in rows if r.get("ticker")][:150]
        tick_cache = {c["ticker"]: {k: c.get(k) for k in ("consenso_eps", "whisper", "asimetria_pct", "earnings_fecha",
                                                         "earnings_timing", "beat_rate", "reaccion_media_pct", "score")}
                      for c in evaluados}
        res_cache = [{k: r.get(k) for k in ("ticker", "epsDate", "eps", "estimate", "whisper", "revenue",
                                            "revenueEstimate", "earningsSurprise", "revenueSurprise", "subject")}
                     for r in ew_results_today()][:80]
    except Exception as e:           # la caché nunca debe romper el pipeline
        log(f"  ew_cache incompleta: {e}")

    def mutate(pl):
        kept = [c for c in pl.get("candidatos_validados", [])
                if not (c.get("fuente") == FUENTE and c.get("estado") in PEND)]
        kept_syms = {(c.get("ticker") or c.get("symbol") or "").upper() for c in kept if c.get("estado") in PEND}
        pl["candidatos_validados"] = kept + [c for c in cands[:15] if c["score"] >= 6 and c["ticker"] not in kept_syms]
        pl["fuentes_estado"] = {"ts": ahora.strftime("%Y-%m-%dT%H:%MZ"),
                                **{k: {"ok": ok, "detalle": det} for k, (ok, det) in salud.items()}}
        ewp = pl.setdefault("ew_pipeline", {})
        ewp["acceso_correcto"] = ("API JSON https://www.earningswhispers.com/api/... SIN login. OBLIGATORIO cabecera "
                                  "Referer: https://www.earningswhispers.com/ (sin ella responde HTTP 204 vacío). "
                                  "NUNCA scrapear HTML ni old.earningswhispers.com (muerto). Implementación de "
                                  "referencia: scripts/datasources.py del repo.")
        ewp["headers_requeridos"] = {"User-Agent": "Mozilla/5.0 ... Chrome/128 Safari/537.36",
                                     "Accept": "application/json, text/plain, */*",
                                     "Referer": "https://www.earningswhispers.com/",
                                     "X-Requested-With": "XMLHttpRequest"}
        ewp["ultimo_acceso_ok"] = salud.get("earningswhispers", (False,))[0]
        ewp["si_no_puedes_llamar_a_la_api"] = ("Si tu entorno no tiene shell o no puede enviar la cabecera Referer "
                                              "(p.ej. fetch desde navegador), NO consultes earningswhispers.com: usa "
                                              "la sección ew_cache de este log, que GitHub Actions actualiza cada hora "
                                              "en sesión y cada noche. No es un fallo de EW; no bloquees la tarea.")
        pl["ew_cache"] = {"_doc": "Datos de EarningsWhispers obtenidos por GitHub Actions (API con Referer). Úsalos si "
                                  "no puedes llamar a la API. calendario: releaseTime 1=BMO, 3=AMC; total=nº analistas.",
                          "ts": ahora.strftime("%Y-%m-%dT%H:%MZ"), "calendario": cal_cache,
                          "tickers": tick_cache, "resultados_hoy": res_cache}
        pl["ew_pipeline_gha"] = {"ts": ahora.strftime("%Y-%m-%dT%H:%MZ"), "entrada": E.isoformat(),
                                 "salida": X.isoformat(), "eventos": len(tick), "evaluados": len(evaluados),
                                 "macro": macro, "top": [{k: c[k] for k in ("ticker", "score", "earnings_fecha",
                                                                           "earnings_timing", "score_breakdown")}
                                                         for c in cands[:10]]}

    update_log(mutate, f"ew-pipeline [{ahora:%Y-%m-%dT%H:%M}Z] {len(evaluados)} evaluados")

    malas = [k for k, (ok, _) in salud.items() if not ok]
    lines = [f"📡 EW PIPELINE · entrada {E:%d/%m} · salida {X:%d/%m} · {len(tick)} eventos · {len(evaluados)} evaluados",
             "Fuentes: " + ("✅ todas OK" if not malas else "⚠️ fallan " + ", ".join(malas)),
             f"Macro: VIX {macro.get('vix')} · SPY>MA50 {macro.get('spy_above_ma50')}"]
    for c in cands[:6]:
        tag = "🟢REAL+PAPER" if c["score"] >= 9 else "🔵PAPER" if c["score"] >= 8 else "⚪"
        lines.append(f"{tag} {c['ticker']} {c['score']:.1f} ({c['earnings_timing']} {c['earnings_fecha'][5:]}) "
                     f"BR {c['beat_rate']} asym {c['asimetria_pct']}")
    if not cands:
        lines.append("Sin eventos que cumplan filtros para la próxima sesión.")
    tg("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"🔴 ew-pipeline ERROR: {type(e).__name__}: {e}")
        raise
