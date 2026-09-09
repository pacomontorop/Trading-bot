#!/usr/bin/env python3
"""
Market Open Execution — GitHub Actions
Corre a las 13:40 UTC (15:40 CEST / 9:40 ET) L-V
Independiente de Cowork. Lee performance_log.json, ejecuta candidatos en Alpaca.
"""
import os, json, base64, urllib.request, urllib.error, re, sys, time
from datetime import datetime, timezone, date

# ── CREDENCIALES (desde GitHub Secrets) ─────────────────────────────────────
APCA_KEY    = os.environ["APCA_KEY"]
APCA_SEC    = os.environ["APCA_SEC"]
APCA_BASE   = "https://paper-api.alpaca.markets/v2"
APCA_KEY_R  = os.environ["APCA_KEY_R"]
APCA_SEC_R  = os.environ["APCA_SEC_R"]
APCA_BASE_R = "https://api.alpaca.markets/v2"
GH_TOKEN    = os.environ["GH_TOKEN"]
TG_TOKEN    = os.environ["TG_TOKEN"]
TG_CHAT     = os.environ["TG_CHAT"]
GH_REPO     = "pacomontorop/Trading-bot"
GH_PATH     = "performance_log.json"

import subprocess
subprocess.run([sys.executable, "-m", "pip", "install", "yfinance", "-q"], capture_output=True)
import yfinance as yf

H  = {"APCA-API-KEY-ID": APCA_KEY,   "APCA-API-SECRET-KEY": APCA_SEC}
HR = {"APCA-API-KEY-ID": APCA_KEY_R, "APCA-API-SECRET-KEY": APCA_SEC_R}

def tg(msg):
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT, "text": msg[:4000]}).encode(),
            headers={"Content-Type": "application/json"}), timeout=8)
    except: pass

def alpaca(path, method="GET", data=None, base=None):
    b = base or APCA_BASE
    headers = H if b == APCA_BASE else HR
    if data:
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(b + path,
        data=json.dumps(data).encode() if data else None,
        headers=headers, method=method)
    try:
        body = urllib.request.urlopen(req, timeout=15).read()
        return json.loads(body) if body.strip() else {"_ok": True}
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:200]
        print(f"  HTTP {e.code} {path}: {err[:80]}")
        return {"_error": err, "_code": e.code}
    except Exception as e:
        print(f"  ERR {path}: {e}")
        return {"_error": str(e)}

# ── LEER PERFORMANCE LOG ────────────────────────────────────────────────────
def read_log():
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
    req = urllib.request.Request(url,
        headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    sha = resp["sha"]
    content = json.loads(base64.b64decode(resp["content"]))
    return content, sha

def write_log(plog, sha):
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
    body = {
        "message": f"market-open-execution [{datetime.utcnow().strftime('%Y-%m-%dT%H:%M')}Z]",
        "content": base64.b64encode(json.dumps(plog, ensure_ascii=False, indent=2).encode()).decode(),
        "sha": sha
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Authorization": f"token {GH_TOKEN}", "Content-Type": "application/json"}, method="PUT")
    resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
    return resp["content"]["sha"]

# ── MAIN ────────────────────────────────────────────────────────────────────
print(f"=== market-open-execution {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")
ahora = datetime.now(timezone.utc)

# Check market open
clock = alpaca("/clock")
if not clock.get("is_open"):
    msg = f"market-open-execution (GitHub Actions): MERCADO CERRADO ({ahora.strftime('%H:%M UTC')}) — no hay ordenes."
    tg(msg); print(msg); sys.exit(0)

print("Mercado ABIERTO ✅")
plog, sha = read_log()
params = plog.get("parametros_activos", {})
acct  = alpaca("/account")
equity = float(acct["equity"])
buying_power = float(acct["buying_power"])
positions = alpaca("/positions")
orders = alpaca("/orders?status=open&limit=50")
print(f"Equity: ${equity:,.0f} | BP: ${buying_power:,.0f}")

ALLOW_KW = ("entrar","largo","comprar","buy","long","open","apertura","premarket")
BLOCK_KW = ("no_entrar","no entrar","descartar","evitar","short","vigilar_no")

def es_entrada(accion):
    a = (accion or "").lower()
    if any(b in a for b in BLOCK_KW): return False
    return any(k in a for k in ALLOW_KW)

def expira_ok(c):
    exp = c.get("expira") or ""
    if not exp: return True
    try: return datetime.fromisoformat(exp.replace("Z", "+00:00")) > ahora
    except: return True

ESTADOS = {"pendiente","pendiente_reentrada","pendiente_ew"}
candidatos = [c for c in plog.get("candidatos_validados", [])
              if c.get("estado") in ESTADOS and expira_ok(c) and es_entrada(c.get("accion_recomendada",""))]
print(f"Candidatos activos: {[c['ticker'] for c in candidatos]}")

if not candidatos:
    tg("market-open (GH Actions): sin candidatos hoy"); sys.exit(0)

simbolos_en_uso = {p["symbol"] for p in (positions if isinstance(positions,list) else [])}
SCORE_MIN_PAPER = float(params.get("score_min_paper", 8.0))
SCORE_MIN_REAL  = float(params.get("score_threshold_real_account", params.get("score_min_real", 8.5)))
MAX_COWORK = int(params.get("max_cowork_positions", 4))
cowork_abiertas = 0

ejecutados = []; descartados = []

for c in candidatos:
    ticker = c["ticker"]
    score  = c.get("score_ajustado", c.get("score", 0))
    print(f"\n── {ticker} (score {score:.1f}) ──")
    sizing_override = 1.0

    if score < SCORE_MIN_PAPER:
        descartados.append((ticker, score, f"score {score:.1f} < {SCORE_MIN_PAPER}")); continue
    if ticker in simbolos_en_uso:
        descartados.append((ticker, score, "ya tiene posicion")); continue
    if cowork_abiertas >= MAX_COWORK:
        descartados.append((ticker, score, f"limite {MAX_COWORK}")); continue

    # Runup check
    _runup_high = float(params.get("runup_max_pct_high_score", 8.0))
    _runup_low  = float(params.get("runup_max_pct_low_score", 5.0))
    _score_ovr  = float(params.get("score_runup_override", 8.5))
    _runup_cand = float(c.get("runup_tolerance_pct", _runup_low))
    _runup_thresh = _runup_high if score >= _score_ovr else _runup_cand

    try:
        hist = yf.Ticker(ticker).history(period="2d", interval="5m")
        close_ayer = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else 0
        price_now  = float(hist["Close"].iloc[-1]) if len(hist) >= 1 else 0
        move = (price_now - close_ayer) / close_ayer * 100 if close_ayer else 0
        print(f"  Move vs ayer: {move:+.2f}% (thresh {_runup_thresh:.0f}%)")
    except Exception as e:
        print(f"  precio err: {e}"); move = 0; price_now = 0

    if move > _runup_thresh:
        if score >= _score_ovr:
            sizing_override = 0.50
            print(f"  runup alto pero score alto → sizing 50%")
        else:
            descartados.append((ticker, score, f"run-up {move:.1f}% > {_runup_thresh:.0f}%")); continue

    if buying_power < 500:
        descartados.append((ticker, score, f"BP ${buying_power:.0f} insuf")); continue

    # Sizing
    try:
        hist_d = yf.Ticker(ticker).history(period="20d")
        price = float(hist_d["Close"].iloc[-1])
    except:
        price = float(c.get("precio_referencia", price_now or 100))

    _pct = float(params.get("paper_position_pct", 0.07))
    notional = equity * _pct * sizing_override
    qty = max(int(notional / price), 1)
    stop_pct = float(c.get("stop_pct", 0.05))
    tp_pct   = float(c.get("tp_pct", 0.08))
    stop_price  = round(price * (1 - stop_pct), 2)
    stop_limit  = round(stop_price * 0.995, 2)
    take_profit = round(price * (1 + tp_pct), 2)
    rr = (take_profit - price) / (price - stop_price) if price > stop_price else 1.5

    if rr < 1.10:
        descartados.append((ticker, score, f"R:R {rr:.2f} bajo")); continue
    if buying_power < notional * 1.1:
        descartados.append((ticker, score, f"BP insuf")); continue

    # Orden paper
    try:
        cid = f"GHA-{date.today().strftime('%Y%m%d')}-{ticker}"
        resp = alpaca("/orders", "POST", {
            "symbol": ticker, "qty": str(qty), "side": "buy",
            "type": "market", "time_in_force": "gtc",
            "order_class": "bracket", "client_order_id": cid,
            "take_profit": {"limit_price": str(take_profit)},
            "stop_loss": {"stop_price": str(stop_price), "limit_price": str(stop_limit)}
        })
        oid = resp.get("id", "N/A")
        if "_error" in resp:
            descartados.append((ticker, score, resp["_error"][:60])); continue
        print(f"  PAPER: {qty}acc @ ${price:.2f} stop ${stop_price:.2f} TP ${take_profit:.2f} R:R {rr:.1f}x")
        ejecutados.append({"ticker":ticker,"qty":qty,"price":price,"stop":stop_price,
                           "tp":take_profit,"rr":rr,"score":score,"oid":oid,"sizing_override":sizing_override})
        cowork_abiertas += 1

        # Actualizar estado candidato
        for cv in plog.get("candidatos_validados", []):
            if cv.get("ticker") == ticker and cv.get("estado") in ESTADOS:
                cv["estado"] = "ejecutado"; cv["orden_id"] = oid
                cv["precio_ejecucion"] = price
                cv["fecha_ejecucion"] = ahora.strftime("%Y-%m-%d %H:%M")
                break

        plog.setdefault("operaciones", []).append({
            "id": cid, "fecha_entrada": date.today().isoformat(),
            "simbolo": ticker, "tipo": "GH_Actions",
            "score_entrada": score, "precio_entrada": price,
            "precio_stop": stop_price, "precio_tp": take_profit,
            "qty": qty, "rr": round(rr,2), "estado": "abierta", "orden_id": oid
        })

        # Cuenta real si score suficiente
        if score >= SCORE_MIN_REAL:
            try:
                acct_r = alpaca("/account", base=APCA_BASE_R)
                eq_r = float(acct_r.get("equity", 0))
                bp_r = float(acct_r.get("buying_power", 0))
                _real_pct = float(params.get("real_position_pct", 0.12))
                qty_r = max(int(eq_r * _real_pct * sizing_override / price), 1)
                if bp_r >= price * qty_r * 1.1:
                    cid_r = f"GHA-REAL-{date.today().strftime('%Y%m%d')}-{ticker}"
                    resp_r = alpaca("/orders", "POST", {
                        "symbol": ticker, "qty": str(qty_r), "side": "buy",
                        "type": "market", "time_in_force": "gtc",
                        "order_class": "bracket", "client_order_id": cid_r,
                        "take_profit": {"limit_price": str(take_profit)},
                        "stop_loss": {"stop_price": str(stop_price), "limit_price": str(stop_limit)}
                    }, base=APCA_BASE_R)
                    print(f"  REAL: {qty_r}acc {ticker}")
                    ejecutados[-1]["real_qty"] = qty_r
            except Exception as e:
                print(f"  real err: {e}")
    except Exception as e:
        print(f"  orden err: {e}")
        descartados.append((ticker, score, str(e)[:60]))

# Guardar log
try:
    sha = write_log(plog, sha)
    print(f"\nLog guardado ({sha[:8]})")
except Exception as e:
    print(f"  log write err: {e}")

# Telegram
lines = [f"🚀 APERTURA GH Actions — {date.today().strftime('%d/%m/%Y')}"]
if ejecutados:
    lines.append(f"✅ EJECUTADOS ({len(ejecutados)}):")
    for e in ejecutados:
        r_tag = f" + REAL {e['real_qty']}acc" if e.get("real_qty") else ""
        lines.append(f"  {e['ticker']}: {e['qty']}acc{r_tag} @ ${e['price']:.2f} | stop ${e['stop']:.2f} | TP ${e['tp']:.2f} | R:R {e['rr']:.1f}x | score {e['score']:.1f}")
else:
    lines.append("Sin ejecuciones")
if descartados:
    lines.append(f"DESCARTADOS ({len(descartados)}):")
    for t,s,r in descartados: lines.append(f"  {t}: {r}")
tg("\n".join(lines))
print("✅ Telegram enviado")
