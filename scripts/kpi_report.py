#!/usr/bin/env python3
"""
KPI Report — GitHub Actions, diario tras el cierre (v1, 2026-09-10).

Fuente de verdad = Alpaca (no las cifras que escriben las tareas Cowork):
  • Curva de equity paper/real desde fecha_inicio vs SPY en el MISMO periodo.
  • Operaciones cerradas reconstruidas por FIFO desde los fills de Alpaca
    (acciones y crypto por separado): win rate, profit factor, ganancia/pérdida
    media, expectativa por operación, max drawdown.
  • promotion gate: si las últimas N operaciones de ACCIONES en paper cumplen
    los mínimos de config/risk_limits.json → gate_real_ok = true, lo que
    habilita la cuenta real cuando real.enabled = "auto".

Escribe kpis.rendimiento_verificado y corrige kpis.rendimiento_global.{spy,alpha}.
"""
import collections
from datetime import datetime, timezone

from common import (DRY_RUN, enforce_guardrails, load_limits, log, now_utc, paper_client, real_client, tg, update_log, read_log)

DEFAULT_START = "2026-04-13"


def equity_curve(alp, start):
    h = alp.req("/account/portfolio/history?period=1A&timeframe=1D")
    pts = []
    for t, e in zip(h.get("timestamp", []) or [], h.get("equity", []) or []):
        if e:
            d = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
            if d >= start:
                pts.append((d, float(e)))
    return pts


def max_dd(vals):
    peak, dd = -1e18, 0.0
    for v in vals:
        peak = max(peak, v); dd = min(dd, (v / peak - 1) * 100)
    return round(dd, 2)


def spy_return(alp, start):
    r = alp.data(f"/stocks/SPY/bars?timeframe=1Day&start={start}&feed=iex&limit=1000&adjustment=all")
    bars = (r or {}).get("bars") or []
    if len(bars) < 2:
        return None
    return round((bars[-1]["c"] / bars[0]["o"] - 1) * 100, 2)


def fills(alp, start):
    out, tok = [], None
    for _ in range(50):
        q = f"/account/activities/FILL?direction=asc&page_size=100&after={start}T00:00:00Z"
        if tok:
            q += f"&page_token={tok}"
        r = alp.req(q)
        if not isinstance(r, list) or not r:
            break
        out += r; tok = r[-1]["id"]
        if len(r) < 100:
            break
    return out


def selftest_ids(alp, start):
    """ids de órdenes de scripts/selftest.py (client_order_id SELFTEST-*) para excluir sus fills."""
    ids, after = set(), f"{start}T00:00:00Z"
    for _ in range(40):
        r = alp.req(f"/orders?status=all&limit=500&direction=asc&nested=true&after={after}")
        if not isinstance(r, list) or not r:
            break
        for o in r:
            if (o.get("client_order_id") or "").startswith("SELFTEST-"):
                ids.add(o["id"]); ids.update(l["id"] for l in (o.get("legs") or []))
        if len(r) < 500:
            break
        after = r[-1]["submitted_at"]
    return ids


def closed_trades(fs):
    """FIFO → lista de (fecha, símbolo, pnl, es_crypto) agregada por símbolo y día de cierre."""
    lots = collections.defaultdict(list)
    agg = collections.OrderedDict()
    for a in fs:
        s = a["symbol"]; q = float(a["qty"]); px = float(a["price"])
        sq = q if a["side"] == "buy" else -q
        L = lots[s]
        while abs(sq) > 1e-12 and L and (L[0][0] > 0) != (sq > 0):
            lq, lp = L[0]; m = min(abs(lq), abs(sq))
            pnl = (px - lp) * m if lq > 0 else (lp - px) * m
            k = (a["transaction_time"][:10], s)
            agg[k] = agg.get(k, 0.0) + pnl
            lq = lq - m if lq > 0 else lq + m
            sq = sq - m if sq > 0 else sq + m
            if abs(lq) < 1e-12:
                L.pop(0)
            else:
                L[0] = (lq, lp)
        if abs(sq) > 1e-12:
            L.append((sq, px))
    return [(d, s, p, ("/" in s or (s.endswith("USD") and len(s) > 5))) for (d, s), p in agg.items()]


def stats(tr):
    v = [t[2] for t in tr]
    if not v:
        return {"n": 0}
    w = [x for x in v if x > 0]; l = [x for x in v if x <= 0]
    pf = (sum(w) / -sum(l)) if l and sum(l) < 0 else (99.0 if w else 0.0)
    return {"n": len(v), "win_rate_pct": round(100 * len(w) / len(v), 1), "profit_factor": round(pf, 2),
            "avg_win_usd": round(sum(w) / len(w), 2) if w else 0, "avg_loss_usd": round(sum(l) / len(l), 2) if l else 0,
            "expectancy_usd": round(sum(v) / len(v), 2), "net_usd": round(sum(v), 2)}


def main():
    ahora = now_utc()
    log(f"=== kpi-report {ahora:%Y-%m-%d %H:%M} UTC {'[DRY_RUN]' if DRY_RUN else ''} ===")
    P, R = paper_client(), real_client()
    G = load_limits()["promotion_gate"]
    plog, _ = read_log()
    start = ((plog.get("kpis") or {}).get("rendimiento_global") or {}).get("fecha_inicio") or DEFAULT_START

    spy = spy_return(P, start)
    out = {"_meta": "Calculado por scripts/kpi_report.py desde Alpaca (fuente de verdad). No editar a mano.",
           "actualizado_utc": ahora.strftime("%Y-%m-%dT%H:%MZ"), "desde": start, "spy_pct": spy}

    for name, alp in (("paper", P), ("real", R)):
        if alp is None:
            continue
        cur = equity_curve(alp, start)
        if cur:
            ret = round((cur[-1][1] / cur[0][1] - 1) * 100, 2)
            out[name] = {"equity_inicio": round(cur[0][1], 2), "equity_actual": round(cur[-1][1], 2),
                         "retorno_pct": ret, "alpha_vs_spy_pct": round(ret - spy, 2) if spy is not None else None,
                         "max_drawdown_pct": max_dd([v for _, v in cur])}
        skip = selftest_ids(alp, start)
        tr = closed_trades([f for f in fills(alp, start) if f.get("order_id") not in skip])
        eq_tr = [t for t in tr if not t[3]]; cr_tr = [t for t in tr if t[3]]
        out.setdefault(name, {})["acciones"] = stats(eq_tr)
        out[name]["crypto"] = stats(cr_tr)
        out[name]["acciones_ultimas_%d" % G["lookback_trades"]] = stats(eq_tr[-G["lookback_trades"]:])
        if name == "paper":
            last = stats(eq_tr[-G["lookback_trades"]:])
            dd = out["paper"].get("max_drawdown_pct", 0)
            from datetime import timedelta
            cut = (ahora - timedelta(days=G["return_lookback_days"])).strftime("%Y-%m-%d")
            win = [v for d, v in cur if d >= cut] if cur else []
            r_lb = round((win[-1] / win[0] - 1) * 100, 2) if len(win) >= 2 else None
            out["paper"]["retorno_%dd_pct" % G["return_lookback_days"]] = r_lb
            checks = {"n>=min_trades": last.get("n", 0) >= G["min_trades"],
                      "equity_%dd>=min" % G["return_lookback_days"]: r_lb is not None and r_lb >= G["min_paper_return_lookback_pct"],
                      "PF>=min": last.get("profit_factor", 0) >= G["min_profit_factor"],
                      "expectancy>min": last.get("expectancy_usd", -1) > G["min_expectancy_usd"],
                      "drawdown_ok": dd >= -G["max_drawdown_pct"]}
            out["gate_checks"] = checks
            out["gate_real_ok"] = all(checks.values())

    guard = []

    def mutate(pl):
        guard[:] = enforce_guardrails(pl)
        k = pl.setdefault("kpis", {})
        k["rendimiento_verificado"] = out
        rg = k.setdefault("rendimiento_global", {})
        if spy is not None:
            rg["spy_mismo_periodo_pct"] = spy
            if "paper" in out and "retorno_pct" in out["paper"]:
                rg["alpha_paper_vs_spy_pct"] = out["paper"]["alpha_vs_spy_pct"]
            if "real" in out and "retorno_pct" in out["real"]:
                rg["alpha_real_vs_spy_pct"] = out["real"]["alpha_vs_spy_pct"]
            rg["_nota_spy"] = "Sobrescrito por kpi_report.py con datos Alpaca (SPY ajustado, mismo periodo)."

    update_log(mutate, f"kpi-report [{ahora:%Y-%m-%d}] verificado Alpaca")

    pa, re_ = out.get("paper", {}), out.get("real", {})
    s = pa.get("acciones_ultimas_%d" % G["lookback_trades"], {})
    msg = [f"📈 KPIs VERIFICADOS desde {start} | SPY {spy:+.2f}%" if spy is not None else "📈 KPIs VERIFICADOS",
           f"Paper: {pa.get('retorno_pct', 0):+.2f}% (alpha {pa.get('alpha_vs_spy_pct', 0):+.2f}) DD {pa.get('max_drawdown_pct', 0)}%",
           f"Real:  {re_.get('retorno_pct', 0):+.2f}% (alpha {re_.get('alpha_vs_spy_pct', 0):+.2f})",
           f"Últimas {s.get('n', 0)} ops acciones paper: WR {s.get('win_rate_pct', 0)}% PF {s.get('profit_factor', 0)} "
           f"E={s.get('expectancy_usd', 0):+.0f}$ (win {s.get('avg_win_usd', 0):.0f} / loss {s.get('avg_loss_usd', 0):.0f})",
           f"Gate cuenta real: {'🟢 OK' if out.get('gate_real_ok') else '🔴 NO'} {out.get('gate_checks', {})}"]
    if guard:
        msg.append("🛡️ Guardrails re-aplicados: " + "; ".join(guard))
    tg("\n".join(msg))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"🔴 kpi-report ERROR: {type(e).__name__}: {e}")
        raise
