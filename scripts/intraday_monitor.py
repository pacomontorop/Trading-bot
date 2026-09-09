#!/usr/bin/env python3
"""
Intraday Monitor + Position Manager — GitHub Actions
Corre a 12:30 ET (16:30 UTC) y 15:00 ET (19:00 UTC).
Gestiona posiciones abiertas: trailing stops, cierre por TP/SL, nuevas entradas si hay señal.
"""
import os, json, base64, urllib.request, urllib.error, sys, subprocess
from datetime import datetime, timezone, date, timedelta

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

subprocess.run([sys.executable,"-m","pip","install","yfinance","-q"], capture_output=True)
import yfinance as yf

H  = {"APCA-API-KEY-ID": APCA_KEY,   "APCA-API-SECRET-KEY": APCA_SEC}
HR = {"APCA-API-KEY-ID": APCA_KEY_R, "APCA-API-SECRET-KEY": APCA_SEC_R}
ahora = datetime.now(timezone.utc)

def tg(msg):
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id":TG_CHAT,"text":msg[:4000]}).encode(),
            headers={"Content-Type":"application/json"}), timeout=8)
    except: pass

def alpaca(path, method="GET", data=None, base=None):
    b = base or APCA_BASE
    h = H if b == APCA_BASE else HR
    if data: h = {**h, "Content-Type":"application/json"}
    req = urllib.request.Request(b+path, data=json.dumps(data).encode() if data else None,
        headers=h, method=method)
    try:
        body = urllib.request.urlopen(req, timeout=15).read()
        return json.loads(body) if body.strip() else {"_ok":True}
    except urllib.error.HTTPError as e:
        return {"_error":e.read().decode()[:200],"_code":e.code}
    except Exception as e:
        return {"_error":str(e)}

def read_log():
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
    req = urllib.request.Request(url, headers={"Authorization":f"token {GH_TOKEN}",
        "Accept":"application/vnd.github.v3+json"})
    resp = json.loads(urllib.request.urlopen(req,timeout=15).read())
    return json.loads(base64.b64decode(resp["content"])), resp["sha"]

def write_log(plog, sha):
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
    body={"message":f"intraday-monitor [{ahora.strftime('%Y-%m-%dT%H:%M')}Z]",
          "content":base64.b64encode(json.dumps(plog,ensure_ascii=False,indent=2).encode()).decode(),"sha":sha}
    req=urllib.request.Request(url,data=json.dumps(body).encode(),
        headers={"Authorization":f"token {GH_TOKEN}","Content-Type":"application/json"},method="PUT")
    return json.loads(urllib.request.urlopen(req,timeout=20).read())["content"]["sha"]

print(f"=== intraday-monitor {ahora.strftime('%Y-%m-%d %H:%M UTC')} ===")

clock = alpaca("/clock")
if not clock.get("is_open"):
    tg(f"intraday-monitor: mercado cerrado {ahora.strftime('%H:%M UTC')}"); sys.exit(0)

positions = alpaca("/positions")
acct = alpaca("/account")
equity = float(acct.get("equity",0))
print(f"Equity: ${equity:,.0f} | Posiciones: {len(positions if isinstance(positions,list) else [])}")

plog, sha = read_log()
params = plog.get("parametros_activos",{})
alerts = []

# ── GESTIÓN DE POSICIONES EXISTENTES ─────────────────────────────────────────
for pos in (positions if isinstance(positions,list) else []):
    sym   = pos["symbol"]
    qty   = float(pos["qty"])
    price = float(pos.get("current_price",0))
    cost  = float(pos.get("avg_entry_price",0))
    pnl   = float(pos.get("unrealized_pl",0))
    pnlpc = float(pos.get("unrealized_plpc",0))*100
    print(f"\n  {sym}: qty={qty} price=${price:.2f} pnl={pnlpc:+.1f}% (${pnl:+.0f})")

    # Hard stop: -12% emergency close
    if pnlpc < -12 and qty > 0:
        print(f"    🚨 EMERGENCY CLOSE {sym}: {pnlpc:.1f}%")
        # Cancel open orders
        open_orders = alpaca(f"/orders?status=open&symbols={sym}&limit=5")
        for o in (open_orders if isinstance(open_orders,list) else []):
            alpaca(f"/orders/{o['id']}", method="DELETE")
        alpaca("/orders","POST",{"symbol":sym,"qty":str(int(qty)),"side":"sell",
            "type":"market","time_in_force":"day"})
        alerts.append(f"🚨 CIERRE EMERGENCIA {sym}: {pnlpc:.1f}% (>${pnl:+.0f})")
        continue

    # Trailing stop upgrade: if +5%, move stop to breakeven
    if pnlpc > 5 and cost > 0:
        new_stop = round(cost * 1.005, 2)  # breakeven + 0.5%
        # Check if there's already a stop order
        open_orders = alpaca(f"/orders?status=open&symbols={sym}&limit=10")
        existing_stops = [o for o in (open_orders if isinstance(open_orders,list) else [])
                         if o.get("type") in ("stop","stop_limit") and o.get("side")=="sell"]
        if existing_stops:
            current_stop = float(existing_stops[0].get("stop_price",0))
            if new_stop > current_stop * 1.01:  # only upgrade if meaningfully better
                # Cancel old stop
                alpaca(f"/orders/{existing_stops[0]['id']}", method="DELETE")
                # Place new stop
                alpaca("/orders","POST",{"symbol":sym,"qty":str(int(qty)),"side":"sell",
                    "type":"stop","time_in_force":"gtc","stop_price":str(new_stop)})
                print(f"    📈 TRAILING STOP {sym}: ${current_stop:.2f} → ${new_stop:.2f} (breakeven)")
                alerts.append(f"📈 Trailing stop {sym}: ${new_stop:.2f} (breakeven+0.5%)")

    # Take partial profit at +7%: sell 50%
    if pnlpc > 7 and qty >= 2:
        partial_qty = int(qty * 0.5)
        if partial_qty > 0:
            cid = f"GHA-PARTIAL-{date.today().strftime('%Y%m%d')}-{sym}"
            resp = alpaca("/orders","POST",{"symbol":sym,"qty":str(partial_qty),"side":"sell",
                "type":"market","time_in_force":"day","client_order_id":cid})
            if "_error" not in resp:
                print(f"    💰 PARTIAL PROFIT {sym}: vendido {partial_qty} acc @ ${price:.2f}")
                alerts.append(f"💰 Partial profit {sym}: {partial_qty}acc @ ${price:.2f} | P&L ${pnl*0.5:+.0f}")

# ── REAL ACCOUNT ─────────────────────────────────────────────────────────────
positions_r = alpaca("/positions", base=APCA_BASE_R)
for pos in (positions_r if isinstance(positions_r,list) else []):
    sym   = pos["symbol"]
    qty   = float(pos["qty"])
    pnlpc = float(pos.get("unrealized_plpc",0))*100
    if pnlpc < -12 and qty > 0:
        open_orders_r = alpaca(f"/orders?status=open&symbols={sym}&limit=5", base=APCA_BASE_R)
        for o in (open_orders_r if isinstance(open_orders_r,list) else []):
            alpaca(f"/orders/{o['id']}", method="DELETE", base=APCA_BASE_R)
        alpaca("/orders","POST",{"symbol":sym,"qty":str(int(qty)),"side":"sell",
            "type":"market","time_in_force":"day"}, base=APCA_BASE_R)
        alerts.append(f"🚨 REAL CIERRE EMERGENCIA {sym}: {pnlpc:.1f}%")

# ── NUEVAS ENTRADAS — si hay candidatos frescos del scanner ──────────────────
candidatos = [c for c in plog.get("candidatos_validados",[])
              if c.get("estado")=="pendiente"
              and c.get("fuente") in ("dynamic_scanner_gha","live_open_scan")
              and c.get("score",0) >= float(params.get("score_min_paper",8.0))]

pos_syms = {p["symbol"] for p in (positions if isinstance(positions,list) else [])}
MAX = int(params.get("max_cowork_positions",4))
abiertas = len([p for p in (positions if isinstance(positions,list) else [])
                if p["symbol"] not in ("BTCUSD","ETHUSD","SOLUSD")])  # exclude crypto

for c in sorted(candidatos, key=lambda x: x.get("score",0), reverse=True)[:2]:
    ticker = c["ticker"]
    if ticker in pos_syms or abiertas >= MAX: break
    try:
        price = float(yf.Ticker(ticker).history(period="1d")["Close"].iloc[-1])
        pct = float(params.get("paper_position_pct",0.07))
        qty = max(int(equity*pct/price), 1)
        stop_pct = float(c.get("stop_pct",0.05)); tp_pct = float(c.get("tp_pct",0.10))
        stop_p = round(price*(1-stop_pct),2); stop_l = round(stop_p*0.995,2)
        tp_p   = round(price*(1+tp_pct),2)
        cid = f"GHA-INTRA-{date.today().strftime('%Y%m%d')}-{ticker}"
        resp = alpaca("/orders","POST",{"symbol":ticker,"qty":str(qty),"side":"buy",
            "type":"market","time_in_force":"gtc","order_class":"bracket",
            "client_order_id":cid,"take_profit":{"limit_price":str(tp_p)},
            "stop_loss":{"stop_price":str(stop_p),"limit_price":str(stop_l)}})
        if "_error" not in resp:
            alerts.append(f"🆕 NUEVA ENTRADA (intraday) {ticker}: {qty}acc @ ${price:.2f} | s={c['score']:.1f}")
            abiertas += 1
    except Exception as e:
        print(f"  nueva entrada {ticker} err: {e}")

# Save and report
write_log(plog, sha)
msg = f"📊 INTRADAY {ahora.strftime('%H:%M UTC')} | Equity ${equity:,.0f}\n"
if alerts:
    msg += "\n".join(alerts)
else:
    msg += f"✅ Posiciones estables | {abiertas} abiertas | sin acción requerida"
tg(msg); print(msg)
