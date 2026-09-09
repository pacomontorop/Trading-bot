#!/usr/bin/env python3
"""
Market Open Execution — GitHub Actions
13:40 UTC / 15:40 CEST / 9:40 ET lunes-viernes.
1) Corre dynamic_scanner inline para candidatos frescos al momento de apertura.
2) Ejecuta top candidatos scored ≥ 8.0 en Alpaca paper + real.
"""
import os, json, base64, urllib.request, urllib.error, sys, time, subprocess
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
UW_KEY      = os.environ.get("UW_KEY", "")
GH_REPO     = "pacomontorop/Trading-bot"
GH_PATH     = "performance_log.json"

subprocess.run([sys.executable,"-m","pip","install","yfinance","requests","-q"], capture_output=True)
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
    headers = (H if b==APCA_BASE else HR)
    if data: headers = {**headers,"Content-Type":"application/json"}
    req = urllib.request.Request(b+path, data=json.dumps(data).encode() if data else None,
        headers=headers, method=method)
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
    body={"message":f"market-open [{ahora.strftime('%Y-%m-%dT%H:%M')}Z]",
          "content":base64.b64encode(json.dumps(plog,ensure_ascii=False,indent=2).encode()).decode(),"sha":sha}
    req=urllib.request.Request(url,data=json.dumps(body).encode(),
        headers={"Authorization":f"token {GH_TOKEN}","Content-Type":"application/json"},method="PUT")
    return json.loads(urllib.request.urlopen(req,timeout=20).read())["content"]["sha"]

print(f"=== market-open-execution {ahora.strftime('%Y-%m-%d %H:%M UTC')} ===")

# Check market
clock = alpaca("/clock")
if not clock.get("is_open"):
    msg = f"market-open (GHA): MERCADO CERRADO {ahora.strftime('%H:%M UTC')} — no hay ordenes."
    tg(msg); print(msg); sys.exit(0)

acct = alpaca("/account")
equity = float(acct["equity"]); buying_power = float(acct["buying_power"])
positions = alpaca("/positions")
print(f"Mercado ABIERTO | Equity: ${equity:,.0f} | BP: ${buying_power:,.0f}")

# Read log — use candidatos from dynamic_scanner (run 30min before)
plog, sha = read_log()
params = plog.get("parametros_activos",{})

ALLOW_KW=("entrar","largo","comprar","buy","long","open","apertura","premarket")
BLOCK_KW=("no_entrar","no entrar","descartar","evitar","short","vigilar_no")

def es_entrada(a):
    a=(a or "").lower()
    if any(b in a for b in BLOCK_KW): return False
    return any(k in a for k in ALLOW_KW)

def expira_ok(c):
    exp=c.get("expira") or ""
    if not exp: return True
    try: return datetime.fromisoformat(exp.replace("Z","+00:00"))>ahora
    except: return True

ESTADOS={"pendiente","pendiente_reentrada","pendiente_ew"}
candidatos=[c for c in plog.get("candidatos_validados",[])
            if c.get("estado") in ESTADOS and expira_ok(c) and es_entrada(c.get("accion_recomendada",""))]

# Also do a QUICK live momentum check to add/refresh candidates
# (in case scanner ran >2h ago or has stale data)
UNIVERSE_QUICK=["TQQQ","SOXL","NVDA","AMD","MSFT","AAPL","GOOGL","META","AMZN","CRM",
                "DELL","CRWD","PLTR","COIN","SMCI","ARM","MSTR","UPRO","TECL","FNGU",
                "XLK","SMH","NFLX","TSLA","AVGO","MU","AMAT"]
print(f"Quick scan {len(UNIVERSE_QUICK)} tickers para validar frescura...")
live_cands=[]
for ticker in UNIVERSE_QUICK:
    try:
        hist=yf.Ticker(ticker).history(period="3d",interval="5m")
        if len(hist)<10: continue
        close=hist["Close"]; vol=hist["Volume"]
        price=float(close.iloc[-1])
        close_ayer=float(close.iloc[0]) if len(close)>0 else price
        # Get yesterday close from daily
        hd=yf.Ticker(ticker).history(period="3d")
        if len(hd)>=2: close_ayer=float(hd["Close"].iloc[-2])
        ret1d=(price-close_ayer)/close_ayer*100
        vol_now=float(vol.iloc[-3:].mean()); vol_avg=float(vol.mean())
        vol_ratio=vol_now/vol_avg if vol_avg else 1
        # Simple score: momentum + volume
        score=0
        if ret1d>2: score+=3
        elif ret1d>0.5: score+=1.5
        if vol_ratio>2.5: score+=2.5
        elif vol_ratio>1.5: score+=1.2
        # Near 52w high
        high52=float(hd["High"].max()) if len(hd)>0 else price
        pct_hi=(high52-price)/high52*100
        if pct_hi<5: score+=1.5
        if score>=4.0:
            norm=round(min(score/8*10,10),2)
            # Check if already in candidatos
            if not any(c["ticker"]==ticker for c in candidatos):
                live_cands.append({"ticker":ticker,"symbol":ticker,"estado":"pendiente",
                    "score":norm,"score_ajustado":norm,"accion_recomendada":"entrar_apertura",
                    "precio_referencia":round(price,2),"stop_pct":0.05,"tp_pct":0.10,
                    "runup_tolerance_pct":8.0 if norm>=8.5 else 5.0,
                    "expira":(ahora+timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "fuente":"live_open_scan","notas":f"live 1d={ret1d:+.1f}% vol={vol_ratio:.1f}x"})
    except: pass

live_cands.sort(key=lambda x: x["score"],reverse=True)
print(f"Live scan: {len(live_cands)} candidatos frescos")

# Merge: live overrides stale, keep top 6 total
all_cands=candidatos+live_cands[:4]
all_cands.sort(key=lambda x: x.get("score",0),reverse=True)
all_cands=all_cands[:6]
print(f"Total candidatos a evaluar: {len(all_cands)}")

SCORE_MIN_PAPER=float(params.get("score_min_paper",8.0))
SCORE_MIN_REAL =float(params.get("score_threshold_real_account",params.get("score_min_real",8.5)))
MAX_COWORK=int(params.get("max_cowork_positions",4))
simbolos_en_uso={p["symbol"] for p in (positions if isinstance(positions,list) else [])}
cowork_abiertas=0
ejecutados=[]; descartados=[]

for c in all_cands:
    ticker=c["ticker"]; score=c.get("score_ajustado",c.get("score",0))
    print(f"\n── {ticker} (score {score:.1f}) [{c.get('fuente','?')}] ──")
    sizing_override=1.0

    if score<SCORE_MIN_PAPER:
        descartados.append((ticker,score,f"score {score:.1f}<{SCORE_MIN_PAPER}")); continue
    if ticker in simbolos_en_uso:
        descartados.append((ticker,score,"ya posicion")); continue
    if cowork_abiertas>=MAX_COWORK:
        descartados.append((ticker,score,f"limite {MAX_COWORK}")); break

    # Runup check
    rh=float(params.get("runup_max_pct_high_score",8.0))
    rl=float(params.get("runup_max_pct_low_score",5.0))
    so=float(params.get("score_runup_override",8.5))
    rt=c.get("runup_tolerance_pct",rl)
    thresh=rh if score>=so else rt
    try:
        hd=yf.Ticker(ticker).history(period="3d")
        price=float(hd["Close"].iloc[-1])
        close_y=float(hd["Close"].iloc[-2]) if len(hd)>=2 else price
        move=(price-close_y)/close_y*100
        print(f"  Move vs ayer: {move:+.2f}% (thresh {thresh:.0f}%)")
    except:
        price=float(c.get("precio_referencia",100)); move=0
    if move>thresh:
        if score>=so: sizing_override=0.5; print(f"  runup alto→sizing 50%")
        else: descartados.append((ticker,score,f"runup {move:.1f}%>{thresh:.0f}%")); continue

    if buying_power<1000:
        descartados.append((ticker,score,f"BP ${buying_power:.0f}")); continue

    pct=float(params.get("paper_position_pct",0.07))
    notional=equity*pct*sizing_override
    qty=max(int(notional/price),1)
    stop_pct=float(c.get("stop_pct",0.05)); tp_pct=float(c.get("tp_pct",0.10))
    stop_price=round(price*(1-stop_pct),2); stop_limit=round(stop_price*0.995,2)
    take_profit=round(price*(1+tp_pct),2)
    rr=(take_profit-price)/(price-stop_price) if price>stop_price else 1.5
    if rr<1.1: descartados.append((ticker,score,f"R:R {rr:.2f}")); continue
    if buying_power<notional*1.1: descartados.append((ticker,score,"BP insuf")); continue

    try:
        cid=f"GHA-{date.today().strftime('%Y%m%d')}-{ticker}"
        resp=alpaca("/orders","POST",{"symbol":ticker,"qty":str(qty),"side":"buy",
            "type":"market","time_in_force":"gtc","order_class":"bracket",
            "client_order_id":cid,
            "take_profit":{"limit_price":str(take_profit)},
            "stop_loss":{"stop_price":str(stop_price),"limit_price":str(stop_limit)}})
        if "_error" in resp: descartados.append((ticker,score,resp["_error"][:60])); continue
        oid=resp.get("id","N/A")
        print(f"  PAPER: {qty}acc @ ${price:.2f} stop ${stop_price:.2f} TP ${take_profit:.2f} R:R {rr:.1f}x")
        ejecutados.append({"ticker":ticker,"qty":qty,"price":price,"stop":stop_price,
            "tp":take_profit,"rr":rr,"score":score,"oid":oid,"sizing_override":sizing_override})
        cowork_abiertas+=1
        # Update candidato state
        for cv in plog.get("candidatos_validados",[]):
            if cv.get("ticker")==ticker and cv.get("estado") in ESTADOS:
                cv["estado"]="ejecutado"; cv["orden_id"]=oid; cv["precio_ejecucion"]=price
                cv["fecha_ejecucion"]=ahora.strftime("%Y-%m-%d %H:%M"); break
        plog.setdefault("operaciones",[]).append({"id":cid,"fecha_entrada":date.today().isoformat(),
            "simbolo":ticker,"tipo":"GH_Actions_open","score_entrada":score,"precio_entrada":price,
            "precio_stop":stop_price,"precio_tp":take_profit,"qty":qty,"rr":round(rr,2),
            "estado":"abierta","orden_id":oid})
        # Real account
        if score>=SCORE_MIN_REAL:
            try:
                acct_r=alpaca("/account",base=APCA_BASE_R)
                eq_r=float(acct_r.get("equity",0)); bp_r=float(acct_r.get("buying_power",0))
                rp=float(params.get("real_position_pct",0.12))
                qty_r=max(int(eq_r*rp*sizing_override/price),1)
                if bp_r>=price*qty_r*1.1:
                    cid_r=f"GHA-REAL-{date.today().strftime('%Y%m%d')}-{ticker}"
                    resp_r=alpaca("/orders","POST",{"symbol":ticker,"qty":str(qty_r),"side":"buy",
                        "type":"market","time_in_force":"gtc","order_class":"bracket",
                        "client_order_id":cid_r,"take_profit":{"limit_price":str(take_profit)},
                        "stop_loss":{"stop_price":str(stop_price),"limit_price":str(stop_limit)}},
                        base=APCA_BASE_R)
                    print(f"  REAL: {qty_r}acc {ticker}")
                    ejecutados[-1]["real_qty"]=qty_r
            except Exception as e: print(f"  real err: {e}")
    except Exception as e:
        print(f"  err: {e}"); descartados.append((ticker,score,str(e)[:60]))

sha=write_log(plog,sha)
print(f"Log guardado {sha[:8]}")
lines=[f"🚀 APERTURA GHA {date.today().strftime('%d/%m')} {ahora.strftime('%H:%M')}UTC"]
if ejecutados:
    lines.append(f"✅ EJECUTADOS ({len(ejecutados)}):")
    for e in ejecutados:
        rt=f"+REAL {e.get('real_qty','')}acc" if e.get("real_qty") else "paper"
        lines.append(f"  {e['ticker']}: {e['qty']}acc @ ${e['price']:.2f} | stop ${e['stop']:.2f} TP ${e['tp']:.2f} R:R {e['rr']:.1f}x s={e['score']:.1f} [{rt}]")
else:
    lines.append("Sin ejecuciones (score insuficiente o BP)")
if descartados:
    lines.append(f"Descartados: {', '.join(t for t,_,_ in descartados[:5])}")
tg("\n".join(lines)); print("✅ done")
