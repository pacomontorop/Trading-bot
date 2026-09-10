#!/usr/bin/env python3
"""
Watchdog — GitHub Actions, cada hora en sesión (v1, 2026-09-10).

Vigila que el sistema esté vivo aunque Claude/Cowork o Render fallen.
Solo avisa por Telegram si hay problemas:
  CRÍTICO (siempre): cuenta bloqueada · posición de acciones sin stop ·
                      freno de pérdida diaria superado.
  AVISO (14 y 18 UTC): performance_log sin actualizar > log_stale_hours
                      (tareas Cowork caídas) · log > log_size_warn_kb ·
                      Render sin responder (secret RENDER_HEALTH_URL) ·
                      cuenta real sin operar > N días estando activa.
"""
import os
from datetime import datetime, timezone

from common import (GH_PATH, GH_REPO, _gh_headers, day_pnl_pct, effective_limits, http, is_crypto,
                    load_limits, log, now_utc, paper_client, read_log, real_client, tg, LOCAL_LOG)


def main():
    ahora = now_utc()
    P, R = paper_client(), real_client()
    limits = load_limits(); W = limits["watchdog"]
    crit, warn = [], []

    plog, _ = read_log()
    L = effective_limits(plog, limits)

    for name, alp, lim in (("PAPER", P, L["paper"]), ("REAL", R, L["real"])):
        if alp is None:
            continue
        a = alp.account()
        if "_error" in a:
            crit.append(f"{name}: API Alpaca error {a.get('_code')}"); continue
        if a.get("trading_blocked") or a.get("account_blocked") or a.get("status") != "ACTIVE":
            crit.append(f"{name}: cuenta bloqueada/inactiva ({a.get('status')})")
        dd = day_pnl_pct(a)
        if dd <= -lim["daily_loss_stop_pct"]:
            crit.append(f"{name}: día {dd:.2f}% ≤ −{lim['daily_loss_stop_pct']}% (freno activo)")
        stops = {o["symbol"] for o in alp.open_orders()
                 if o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit", "trailing_stop")}
        for p in alp.positions():
            if not is_crypto(p) and p.get("side") == "long" and p["symbol"] not in stops:
                crit.append(f"{name}: {p['symbol']} SIN STOP ({float(p['unrealized_plpc'])*100:+.1f}%)")

    if ahora.hour in (14, 18) or os.environ.get("WATCHDOG_ALL") == "1":
        if not LOCAL_LOG:
            st, c = http(f"https://api.github.com/repos/{GH_REPO}/commits?path={GH_PATH}&per_page=1", headers=_gh_headers())
            if st == 200 and c:
                t = datetime.fromisoformat(c[0]["commit"]["committer"]["date"].replace("Z", "+00:00"))
                h = (ahora - t).total_seconds() / 3600
                if h > W["log_stale_hours"]:
                    warn.append(f"performance_log sin cambios hace {h:.1f} h → ¿tareas Cowork caídas?")
            st, meta = http(f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}", headers=_gh_headers())
            if st == 200 and meta.get("size", 0) / 1024 > W["log_size_warn_kb"]:
                warn.append(f"performance_log {meta['size']/1024:.0f} KB — archivar histórico (límite API 1 MB)")
        url = os.environ.get("RENDER_HEALTH_URL", "")
        if url:
            st, _b = http(url, timeout=30, retries=2)
            if st != 200:
                warn.append(f"Render bot no responde (HTTP {st}) en {url}")
        if R is not None and L["real"]["active"]:
            o = R.req("/orders?status=all&limit=1&direction=desc")
            if isinstance(o, list) and o:
                last = datetime.fromisoformat(o[0]["submitted_at"].replace("Z", "+00:00"))
                days = (ahora - last).days
                if days > W["real_idle_trading_days_warn"] * 7 / 5:
                    warn.append(f"Cuenta real ACTIVA pero sin órdenes desde hace {days} días")

    gate = ((plog.get("kpis") or {}).get("rendimiento_verificado") or {})
    log(f"watchdog {ahora:%H:%M}Z crit={len(crit)} warn={len(warn)} gate_real_ok={gate.get('gate_real_ok')}")
    if crit or warn:
        tg("🐶 WATCHDOG\n" + "\n".join(["🔴 " + x for x in crit] + ["🟠 " + x for x in warn]))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"🔴 watchdog ERROR: {type(e).__name__}: {e}")
        raise
