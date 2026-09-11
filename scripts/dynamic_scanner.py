#!/usr/bin/env python3
"""
Dynamic Scanner — GitHub Actions
Corre cada 30 min en horario de mercado (13:00-22:00 UTC L-V).
Escanea 80+ tickers, scoring multi-fuente, actualiza candidatos en GitHub.
Fuentes: yfinance, Alpaca news, SEC EDGAR insiders, Finviz, Unusual Whales (si disponible).
"""
import os, json, base64, urllib.request, urllib.error, sys, time
from datetime import datetime, timezone, date, timedelta

# ── CREDENCIALES ─────────────────────────────────────────────────────────────
APCA_KEY  = os.environ["APCA_KEY"]
APCA_SEC  = os.environ["APCA_SEC"]
APCA_BASE = "https://paper-api.alpaca.markets/v2"
GH_TOKEN  = os.environ["GH_TOKEN"]
TG_TOKEN  = os.environ["TG_TOKEN"]
TG_CHAT   = os.environ["TG_CHAT"]
UW_KEY    = os.environ.get("UW_KEY", "")   # opcional — añadir secret cuando disponible
GH_REPO   = "pacomontorop/Trading-bot"
GH_PATH   = "performance_log.json"

import subprocess
subprocess.run([sys.executable, "-m", "pip", "install", "yfinance", "requests", "-q"], capture_output=True)
import yfinance as yf
import requests

H = {"APCA-API-KEY-ID": APCA_KEY, "APCA-API-SECRET-KEY": APCA_SEC}
ahora = datetime.now(timezone.utc)

def tg(msg):
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT, "text": msg[:4000]}).encode(),
            headers={"Content-Type": "application/json"}), timeout=8)
    except: pass

def alpaca(path):
    try:
        return json.loads(urllib.request.urlopen(
            urllib.request.Request(APCA_BASE + path, headers=H), timeout=10).read())
    except: return {}

# ── LEER / ESCRIBIR LOG ───────────────────────────────────────────────────────
def read_log():
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
    req = urllib.request.Request(url, headers={"Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    return json.loads(base64.b64decode(resp["content"])), resp["sha"]

def write_log(plog, sha):
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
    body = {"message": f"scanner [{ahora.strftime('%Y-%m-%dT%H:%M')}Z]",
            "content": base64.b64encode(json.dumps(plog, ensure_ascii=False, indent=2).encode()).decode(),
            "sha": sha}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Authorization": f"token {GH_TOKEN}", "Content-Type": "application/json"}, method="PUT")
    resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
    return resp["content"]["sha"]

# ── UNIVERSO DE TICKERS ───────────────────────────────────────────────────────
UNIVERSE = [
    # ETFs apalancados / momentum
    "TQQQ","SOXL","UPRO","FNGU","TECL","LABU","TNA","SPXL","UDOW","NAIL",
    # Tech mega-cap
    "NVDA","AMD","MSFT","AAPL","GOOGL","META","AMZN","CRM","ADBE","ORCL",
    "INTC","QCOM","AVGO","MU","AMAT","KLAC","LRCX","MRVL","SMCI","ARM",
    # Tech mid/growth
    "DELL","HPE","CRWD","DDOG","ZS","NET","SNOW","PANW","OKTA","HUBS",
    "SHOP","COIN","MSTR","PLTR","HOOD","RBLX","U","APP","TTWO",
    # Semis
    "TSM","ASML","ONTO","WOLF","QRVO","SWKS","MPWR",
    # Finanzas
    "JPM","GS","MS","BAC","V","MA","PYPL","SQ","AFRM",
    # Consumer/industrial
    "TSLA","RIVN","NKE","LULU","DECK","CELH","ELF",
    # Biotech/pharma
    "MRNA","BNTX","REGN","VRTX","BIIB","ABBV",
    # ETFs sector
    "XLK","SMH","QQQ","IWM","XBI","ARKK",
]

# ── FUENTE 1: MOMENTUM + VOLUMEN (yfinance) ───────────────────────────────────
def score_momentum(ticker):
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="21d")
        if len(hist) < 5: return 0, {}
        close = hist["Close"]
        vol   = hist["Volume"]
        price  = float(close.iloc[-1])
        ret1d  = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100
        ret5d  = (close.iloc[-1] - close.iloc[-5]) / close.iloc[-5] * 100
        ret20d = (close.iloc[-1] - close.iloc[-20]) / close.iloc[-20] * 100
        vol_avg= float(vol.iloc[-20:-1].mean())
        vol_now= float(vol.iloc[-1])
        vol_ratio = vol_now / vol_avg if vol_avg else 1

        score = 0
        # Momentum scoring
        if ret1d > 3: score += 2.0
        elif ret1d > 1: score += 1.0
        elif ret1d > 0: score += 0.3
        elif ret1d < -3: score -= 2.0
        elif ret1d < -1: score -= 1.0

        if ret5d > 5: score += 1.5
        elif ret5d > 2: score += 0.8
        elif ret5d < -5: score -= 1.5

        if ret20d > 10: score += 1.0
        elif ret20d > 5: score += 0.5
        elif ret20d < -10: score -= 1.0

        # Volume surge
        if vol_ratio > 3: score += 2.5
        elif vol_ratio > 2: score += 1.5
        elif vol_ratio > 1.5: score += 0.8
        elif vol_ratio > 1.2: score += 0.3

        # Price above MAs
        ma5  = float(close.iloc[-5:].mean())
        ma20 = float(close.iloc[-20:].mean())
        if price > ma5 > ma20: score += 1.0
        elif price > ma20: score += 0.5

        # 52-week high proximity
        high52 = float(hist["High"].max())
        pct_from_high = (high52 - price) / high52 * 100
        if pct_from_high < 5: score += 1.5   # near 52w high = breakout zone
        elif pct_from_high < 10: score += 0.5

        return score, {
            "price": round(price, 2),
            "ret1d": round(ret1d, 2),
            "ret5d": round(ret5d, 2),
            "ret20d": round(ret20d, 2),
            "vol_ratio": round(vol_ratio, 2),
            "pct_from_52h": round(pct_from_high, 1)
        }
    except Exception as e:
        return 0, {}

# ── FUENTE 2: NEWS SENTIMENT (Alpaca) ────────────────────────────────────────
NEWS_POS = ["upgrade","beat","raise","buyback","record","breakthrough","approval","partnership",
            "call sweep","unusual options","activist","acquired","merger","special dividend"]
NEWS_NEG = ["downgrade","miss","cut","warning","fraud","lawsuit","probe","dilution",
            "secondary","bankruptcy","sec subpoena","restatement","layoff"]

def score_news(ticker):
    score = 0
    try:
        url = f"https://data.alpaca.markets/v1beta1/news?symbols={ticker}&limit=10&sort=desc"
        news = json.loads(urllib.request.urlopen(
            urllib.request.Request(url, headers=H), timeout=8).read()).get("news", [])
        for n in news[:5]:
            hl = n.get("headline","").lower()
            created = n.get("created_at","")
            # Only last 48h
            try:
                age_h = (ahora - datetime.fromisoformat(created.replace("Z","+00:00"))).total_seconds()/3600
                if age_h > 48: continue
            except: pass
            for p in NEWS_POS:
                if p in hl: score += 0.8; break
            for ng in NEWS_NEG:
                if ng in hl: score -= 1.2; break
    except: pass
    return score

# ── FUENTE 3: SEC EDGAR — INSIDERS ───────────────────────────────────────────
# FIX 2026-09-11: la versión anterior sumaba +0,8 por CUALQUIER Form 4 que mencionara el
# ticker (también ventas y planes de stock), inflando scores con ruido. Los insiders se
# evalúan ahora bien (solo compras P/ventas S en mercado abierto) en fuentes_extra.py,
# sobre los candidatos finales en market_open_execution.py. Aquí aporta 0.
def score_insider(ticker):
    return 0

# ── FUENTE 4: UNUSUAL WHALES (si API key disponible) ─────────────────────────
def score_unusual_whales(ticker):
    if not UW_KEY: return 0
    score = 0
    try:
        url = f"https://api.unusualwhales.com/api/stock/{ticker}/options-volume"
        headers = {"Authorization": f"Bearer {UW_KEY}", "Accept": "application/json"}
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code != 200:
            print(f"  UW {ticker}: HTTP {resp.status_code} {resp.text[:120]}")
            return 0
        data = resp.json().get("data", {})
        if isinstance(data, list):          # la API devuelve lista por fecha → última
            data = data[0] if data else {}
        num = lambda k: float(data.get(k) or 0)
        call_vol, put_vol = num("call_volume"), num("put_volume")
        total = call_vol + put_vol
        if total > 0:
            cp_ratio = call_vol / total
            if cp_ratio > 0.7: score += 2.0
            elif cp_ratio > 0.6: score += 1.0
        if data.get("is_unusual"): score += 1.5
        premium = num("net_premium") or (num("bullish_premium") - num("bearish_premium")) \
                  or (num("net_call_premium") - num("net_put_premium"))
        if premium > 5_000_000: score += 1.5
        elif premium > 1_000_000: score += 0.8
        if not score and total == 0:
            print(f"  UW {ticker}: respuesta sin campos esperados: {list(data)[:12]}")
    except Exception as e:
        print(f"  UW {ticker}: {e}")
    return score

# ── FUENTE 5: SECTOR MOMENTUM ────────────────────────────────────────────────
SECTOR_ETFS = {"tech":"XLK","semis":"SMH","bio":"XBI","fin":"XLF","energy":"XLE","consumer":"XLY"}
_sector_score_cache = {}

def get_sector_bonus(ticker):
    sector_map = {
        "NVDA":"semis","AMD":"semis","INTC":"semis","QCOM":"semis","AVGO":"semis",
        "MU":"semis","AMAT":"semis","KLAC":"semis","LRCX":"semis","SMCI":"semis",
        "SOXL":"semis","SMH":"semis","MSFT":"tech","AAPL":"tech","GOOGL":"tech",
        "META":"tech","CRM":"tech","ADBE":"tech","ORCL":"tech","DELL":"tech",
        "TQQQ":"tech","XLK":"tech","CRWD":"tech","DDOG":"tech","NET":"tech",
        "JPM":"fin","GS":"fin","MS":"fin","BAC":"fin","V":"fin","MA":"fin",
        "XOM":"energy","CVX":"energy","OXY":"energy",
        "MRNA":"bio","BNTX":"bio","REGN":"bio","VRTX":"bio","XBI":"bio",
    }
    sector = sector_map.get(ticker, "other")
    if sector == "other": return 0
    if sector in _sector_score_cache: return _sector_score_cache[sector]
    etf = SECTOR_ETFS.get(sector)
    if not etf: return 0
    try:
        h = yf.Ticker(etf).history(period="2d")
        ret = (float(h["Close"].iloc[-1]) - float(h["Close"].iloc[-2])) / float(h["Close"].iloc[-2]) * 100
        bonus = 0.5 if ret > 1 else (0.2 if ret > 0 else (-0.3 if ret < -1 else 0))
        _sector_score_cache[sector] = bonus
        return bonus
    except: return 0

# ── MAIN SCAN ─────────────────────────────────────────────────────────────────
print(f"=== dynamic_scanner {ahora.strftime('%Y-%m-%d %H:%M UTC')} ===")
print(f"Escaneando {len(UNIVERSE)} tickers...")

# Check market
clock = alpaca("/clock")
is_open = clock.get("is_open", False)
print(f"Mercado: {'ABIERTO' if is_open else 'CERRADO (pre/post-market scan)'}")

# Run scan
resultados = []
for i, ticker in enumerate(UNIVERSE):
    try:
        mom_score, stats = score_momentum(ticker)
        if mom_score < -1: continue  # skip clearly bearish
        news_s   = score_news(ticker)
        insider_s= score_insider(ticker)
        uw_s     = score_unusual_whales(ticker)
        sector_s = get_sector_bonus(ticker)
        total    = mom_score + news_s + insider_s + uw_s + sector_s
        if total >= 3.0:  # only keep meaningful scores
            resultados.append({
                "ticker": total,
                "sym": ticker,
                "score": round(total, 2),
                "momentum": round(mom_score, 2),
                "news": round(news_s, 2),
                "insider": round(insider_s, 2),
                "uw": round(uw_s, 2),
                "sector": round(sector_s, 2),
                "stats": stats
            })
        if i % 20 == 0: print(f"  [{i}/{len(UNIVERSE)}] procesados...")
    except: pass

# Sort by score
resultados.sort(key=lambda x: x["score"], reverse=True)
top = resultados[:8]  # top 8

print(f"\nTop candidatos ({len(top)}):")
for r in top:
    s = r["stats"]
    print(f"  {r['sym']:6s} score={r['score']:.1f} | 1d={s.get('ret1d',0):+.1f}% vol={s.get('vol_ratio',1):.1f}x | mom={r['momentum']:.1f} news={r['news']:.1f} uw={r['uw']:.1f}")

# ── ACTUALIZAR performance_log ────────────────────────────────────────────────
print("\nActualizando performance_log...")
plog, sha = read_log()
params = plog.get("parametros_activos", {})
SCORE_MIN = float(params.get("score_min_paper", 8.0))

# Convert raw scores (our internal 0-15 range) → normalized 0-10
# Our max realistically is ~12, map to 10
def normalize(s): return round(min(s / 12 * 10, 10.0), 2)

# Build new candidatos from top results
now_iso = ahora.isoformat()
expiry  = (ahora + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")  # valid for 6h

# Keep existing ejecutado/abierto candidatos, replace pendiente ones
# FIX 2026-09-10: antes se borraban TODOS los pendientes (incluidos los de la
# pipeline EW/Cowork). Ahora solo se sustituyen los pendientes propios del scanner.
def _es_mio_pendiente(c):
    return c.get("fuente") == "dynamic_scanner_gha" and c.get("estado") in ("pendiente","pendiente_ew","pendiente_reentrada")

new_candidatos = []
for r in top:
    sym   = r["sym"]
    score_norm = normalize(r["score"])
    stats = r["stats"]
    price = stats.get("price", 0)
    if not price: continue

    # Stop/TP based on volatility: use ATR proxy (1d range)
    stop_pct = 0.05 if r["score"] < 7 else 0.04
    tp_pct   = stop_pct * 2.0  # min 2:1 R:R

    # runup tolerance based on score
    runup_pct = 8.0 if r["score"] >= 9 else 5.0

    new_candidatos.append({
        "ticker": sym, "symbol": sym,
        "estado": "pendiente",
        "score": score_norm,
        "score_ajustado": score_norm,
        "score_raw": r["score"],
        "score_breakdown": {k: r[k] for k in ["momentum","news","insider","uw","sector"]},
        "accion_recomendada": "entrar_apertura",
        "precio_referencia": price,
        "stop_pct": stop_pct, "tp_pct": tp_pct,
        "runup_tolerance_pct": runup_pct,
        "expira": expiry,
        "fuente": "dynamic_scanner_gha",
        "fecha_scan": now_iso,
        "notas": f"1d={stats.get('ret1d',0):+.1f}% vol={stats.get('vol_ratio',1):.1f}x 52h_dist={stats.get('pct_from_52h',0):.1f}%"
    })

def _mutate(pl):
    kept = [c for c in pl.get("candidatos_validados", []) if not _es_mio_pendiente(c)]
    kept_syms = {(c.get("ticker") or c.get("symbol") or "").upper() for c in kept
                 if c.get("estado") in ("pendiente","pendiente_ew","pendiente_reentrada")}
    pl["candidatos_validados"] = kept + [c for c in new_candidatos if c["ticker"] not in kept_syms]
    hist = pl.setdefault("scanner_history", [])
    hist.append({"timestamp": now_iso, "tickers_scanned": len(UNIVERSE),
                 "top_results": [{"sym":r["sym"],"score":r["score"]} for r in top[:5]],
                 "uw_active": bool(UW_KEY), "market_open": is_open})
    pl["scanner_history"] = hist[-20:]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import update_log
update_log(_mutate, f"dynamic-scanner [{ahora.strftime('%Y-%m-%dT%H:%M')}Z] {len(new_candidatos)} candidatos")
print("Log actualizado (con reintento ante conflictos)")

# Telegram alert with top picks
sources = "yfinance+news+EDGAR" + ("+UW" if UW_KEY else "")
lines = [f"🔍 SCAN {ahora.strftime('%H:%M UTC')} | {sources} | {'🟢 ABIERTO' if is_open else '🔵 cerrado'}"]
for r in top[:5]:
    s = r["stats"]
    norm = normalize(r["score"])
    lines.append(f"  {r['sym']:6s} {norm:.1f}/10 | 1d={s.get('ret1d',0):+.1f}% vol={s.get('vol_ratio',1):.1f}x | {r['stats'].get('pct_from_52h',0):.0f}% from 52h")
if not UW_KEY:
    lines.append("  ⚡ Añade UW_KEY en GitHub Secrets para señales de opciones")
tg("\n".join(lines))
print("✅ Telegram enviado")
print(f"=== scan completado en {(datetime.now(timezone.utc)-ahora).seconds}s ===")
