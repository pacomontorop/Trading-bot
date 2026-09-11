#!/usr/bin/env python3
"""
fuentes_extra.py — fuentes de datos ADICIONALES para puntuar candidatos (v1, 2026-09-11).

Principios (no negociables):
  1. NINGUNA fuente puede romper ni bloquear la ejecución. Cada llamada va envuelta
     (@fuente), con timeout de red, y el enriquecimiento completo tiene un presupuesto
     de tiempo. Si una fuente falla, su aporte es 0 y el fallo queda en ESTADO.
  2. Aporte acotado: el ajuste total por ticker está en [-AJUSTE_MAX, +AJUSTE_MAX].
  3. Cuenta REAL: estas fuentes solo pueden RESTAR puntos (hasta validarlas en paper).
     Paper usa el ajuste completo para aprender qué fuentes aportan.

Fuentes (todas gratuitas, sin API key):
  insiders       SEC EDGAR, Form 4: compras/ventas en mercado abierto de directivos (30 días)
  analistas      Yahoo: subidas/bajadas de recomendación (30 días) y precio objetivo medio
  opciones       Yahoo: volumen call/put e IV ATM del vencimiento más próximo (≥3 días)
  corto          Yahoo: % del free float vendido en corto (solo penaliza)
  sector         Alpaca: fuerza relativa del ETF del sector frente al SPY (5 y 20 sesiones)
  social         ApeWisdom: menciones en Reddit y su variación en 24 h
  macro          FRED (diferencial high-yield, NFCI, curva 10a-2a) + CNN Fear & Greed

Interruptor: FUENTES_EXTRA=0 desactiva todo (ajuste 0). SEC_UA permite fijar el
User-Agent que exige la SEC ("Nombre email").
Uso manual:  python scripts/fuentes_extra.py AAPL NVDA JPM
"""
from __future__ import annotations

import csv
import functools
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone

ACTIVADA = os.environ.get("FUENTES_EXTRA", "1") != "0"
YF_ENABLED = os.environ.get("NO_YF", "0") != "1"
AJUSTE_MAX = 2.0          # tope del ajuste total por ticker (puntos de score 0-10)
PRESUPUESTO_S = 150       # tiempo máximo del enriquecimiento completo
MAX_TICKERS = 12
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
SEC_UA = os.environ.get("SEC_UA") or "pacomontorop-Trading-bot/1.0 (+https://github.com/pacomontorop/Trading-bot)"

# ── Estado por fuente (para el log y Telegram) ───────────────────────────────
ESTADO: dict = {}
_lock = threading.Lock()


def _marca(nombre: str, ok: bool, err: str | None, ms: float) -> None:
    with _lock:
        e = ESTADO.setdefault(nombre, {"ok": 0, "fallos": 0, "ms": 0, "ultimo_error": None})
        e["ok" if ok else "fallos"] += 1
        e["ms"] += int(ms)
        if not ok and err:
            e["ultimo_error"] = err[:160]


def fuente(nombre: str):
    """Envuelve una fuente: nunca lanza; devuelve None si falla o no hay datos."""
    def deco(fn):
        @functools.wraps(fn)
        def w(*a, **k):
            if not ACTIVADA:
                return None
            t0 = time.time()
            try:
                r = fn(*a, **k)
                _marca(nombre, r is not None, None if r is not None else "sin datos", (time.time() - t0) * 1000)
                return r
            except Exception as e:  # noqa: BLE001 — una fuente jamás tumba el sistema
                _marca(nombre, False, f"{type(e).__name__}: {e}", (time.time() - t0) * 1000)
                return None
        return w
    return deco


def resumen_estado() -> dict:
    with _lock:
        return {k: dict(v) for k, v in ESTADO.items()}


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _get(url: str, headers: dict | None = None, timeout: int = 12, retries: int = 2, as_json: bool = True):
    """GET con reintentos en 429/5xx/red. Devuelve (status, datos|texto|None)."""
    last = (0, None)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if not body.strip():
                    return r.status, None
                return r.status, (json.loads(body) if as_json else body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = (e.code, None)
            if e.code not in (429, 500, 502, 503, 504):
                return last
        except Exception:  # noqa: BLE001
            last = (0, None)
        if i < retries - 1:
            time.sleep(1.5 * (i + 1))
    return last


class _RateLimiter:
    """La SEC permite 10 peticiones/s: dejamos ≥0,15 s entre llamadas (todas las hebras)."""
    def __init__(self, gap: float):
        self.gap, self.t, self.lock = gap, 0.0, threading.Lock()

    def espera(self):
        with self.lock:
            d = self.t + self.gap - time.time()
            if d > 0:
                time.sleep(d)
            self.t = time.time()


_sec_rl = _RateLimiter(0.15)


def _sec_get(url: str, as_json: bool = True, timeout: int = 12):
    _sec_rl.espera()
    return _get(url, {"User-Agent": SEC_UA, "Accept-Encoding": "identity"}, timeout, 2, as_json)


def _num(x, default=None):
    try:
        v = float(x)
        return v if v == v else default      # NaN → default
    except (TypeError, ValueError):
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ═════════════════════════════════════════════════════════════════════════════
# 1 · SEC EDGAR — Form 4 (insiders)
# ═════════════════════════════════════════════════════════════════════════════
_cik_cache: dict = {}
_sub_cache: dict = {}
_cik_fallo: list = []            # si la tabla de CIK falla una vez, no se reintenta en esta ejecución


_cik_lock = threading.Lock()


def cik_de(ticker: str) -> int | None:
    with _cik_lock:                  # una sola descarga de la tabla aunque haya varias hebras
        if _cik_fallo:
            raise RuntimeError(_cik_fallo[0])
        if not _cik_cache:
            st, data = _sec_get("https://www.sec.gov/files/company_tickers.json")
            if isinstance(data, dict):
                for row in data.values():
                    t = str(row.get("ticker", "")).upper()
                    if t:
                        _cik_cache[t] = int(row["cik_str"])
            if not _cik_cache:
                _cik_fallo.append(f"company_tickers.json HTTP {st}")
                raise RuntimeError(_cik_fallo[0])
    return _cik_cache.get(ticker.upper().replace(".", "-"))


def submissions(cik: int) -> dict | None:
    if cik not in _sub_cache:
        st, data = _sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        if not isinstance(data, dict):
            raise RuntimeError(f"submissions HTTP {st}")
        _sub_cache[cik] = data
    return _sub_cache[cik]


def form4_recientes(sub: dict, desde: date, maximo: int = 10) -> list:
    """[(accession, fecha, documento_xml)] de los Form 4 presentados desde `desde`."""
    rec = ((sub or {}).get("filings") or {}).get("recent") or {}
    out = []
    for form, fdate, acc, doc in zip(rec.get("form", []), rec.get("filingDate", []),
                                     rec.get("accessionNumber", []), rec.get("primaryDocument", [])):
        if form != "4":
            continue
        try:
            if date.fromisoformat(fdate) < desde:
                break                              # la lista viene ordenada de más reciente a más antigua
        except ValueError:
            continue
        out.append((acc, fdate, doc.split("/")[-1]))  # "xslF345X05/x.xml" → x.xml (XML original)
        if len(out) >= maximo:
            break
    return out


def parse_form4(xml_text: str) -> list:
    """Transacciones no derivadas de un Form 4: [{owner, cargo, code, shares, price, usd, fecha}]."""
    root = ET.fromstring(xml_text)
    owner = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "?").strip()
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    cargo = []
    if rel is not None:
        if (rel.findtext("isDirector") or "").strip() in ("1", "true"):
            cargo.append("consejero")
        if (rel.findtext("isOfficer") or "").strip() in ("1", "true"):
            cargo.append((rel.findtext("officerTitle") or "directivo").strip())
        if (rel.findtext("isTenPercentOwner") or "").strip() in ("1", "true"):
            cargo.append("10%")
    out = []
    for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = (tx.findtext("transactionCoding/transactionCode") or "").strip()
        sh = _num(tx.findtext("transactionAmounts/transactionShares/value"), 0.0)
        px = _num(tx.findtext("transactionAmounts/transactionPricePerShare/value"), 0.0)
        out.append({"owner": owner, "cargo": ", ".join(cargo), "code": code, "shares": sh, "price": px,
                    "usd": round(sh * px, 2), "fecha": (tx.findtext("transactionDate/value") or "").strip()})
    return out


def resumen_insiders(txs: list) -> dict:
    """Solo cuentan compras (P) y ventas (S) en mercado abierto; subvenciones/ejercicios no."""
    compras = [t for t in txs if t["code"] == "P" and t["usd"] > 0]
    ventas = [t for t in txs if t["code"] == "S" and t["usd"] > 0]
    return {"compras_usd": round(sum(t["usd"] for t in compras)), "ventas_usd": round(sum(t["usd"] for t in ventas)),
            "n_compradores": len({t["owner"] for t in compras}), "n_vendedores": len({t["owner"] for t in ventas}),
            "compradores": sorted({f"{t['owner']} ({t['cargo']})" for t in compras})[:4]}


@fuente("sec_insiders")
def insiders(ticker: str, dias: int = 30, deadline: float | None = None) -> dict | None:
    cik = cik_de(ticker)
    if cik is None:
        return {}                                 # ETF o sin registro en la SEC: no aplica (no es fallo)
    sub = submissions(cik)
    txs, leidos = [], 0
    for acc, fdate, doc in form4_recientes(sub, date.today() - timedelta(days=dias)):
        if deadline and time.time() > deadline:
            break
        st, xml_text = _sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
                                as_json=False)
        if isinstance(xml_text, str) and "<ownershipDocument" in xml_text:
            try:
                txs += parse_form4(xml_text)
                leidos += 1
            except ET.ParseError:
                pass
    r = resumen_insiders(txs)
    r["form4_leidos"] = leidos
    r["sic"] = sub.get("sic")
    return r


def puntua_insiders(d: dict | None) -> float:
    if not d:
        return 0.0
    if d["n_compradores"] >= 2 or d["compras_usd"] >= 1_000_000:
        return 1.0                                # compras en grupo o grandes: la señal más sólida
    if d["compras_usd"] >= 100_000:
        return 0.6
    if d["compras_usd"] > 0:
        return 0.3
    if d["ventas_usd"] >= 20_000_000 and d["n_vendedores"] >= 2:
        return -0.3
    return 0.0


# ═════════════════════════════════════════════════════════════════════════════
# 2-4 · Yahoo: ficha (sector, objetivo, corto), analistas, opciones
# ═════════════════════════════════════════════════════════════════════════════
@fuente("yahoo_ficha")
def ficha_yahoo(ticker: str) -> dict | None:
    if not YF_ENABLED:
        return None
    import yfinance as yf
    info = yf.Ticker(ticker).info or {}
    if not info or len(info) < 5:
        return None
    keys = ("sector", "industry", "quoteType", "currentPrice", "regularMarketPrice", "targetMeanPrice",
            "numberOfAnalystOpinions", "recommendationMean", "shortPercentOfFloat", "shortRatio", "marketCap")
    return {k: info.get(k) for k in keys}


def parse_calificaciones(rows: list, desde: date) -> dict:
    """rows: dicts de yfinance upgrades_downgrades (GradeDate, Firm, Action, priceTargetAction…)."""
    up = down = init = subidas_obj = bajadas_obj = 0
    firmas = []
    for r in rows:
        gd = r.get("GradeDate")
        try:
            d = gd.date() if hasattr(gd, "date") else date.fromisoformat(str(gd)[:10])
        except (ValueError, TypeError):
            continue
        if d < desde:
            continue
        act = str(r.get("Action") or "").lower()
        up += act == "up"
        down += act == "down"
        init += act == "init"
        pta = str(r.get("priceTargetAction") or "").lower()
        subidas_obj += pta.startswith("raise")
        bajadas_obj += pta.startswith("lower")
        if act in ("up", "down", "init"):
            firmas.append(f"{r.get('Firm')}: {act} → {r.get('ToGrade')}")
    return {"subidas": up, "bajadas": down, "inicios": init, "obj_sube": subidas_obj, "obj_baja": bajadas_obj,
            "detalle": firmas[:5]}


@fuente("yahoo_analistas")
def analistas(ticker: str, dias: int = 30) -> dict | None:
    if not YF_ENABLED:
        return None
    import yfinance as yf
    df = yf.Ticker(ticker).upgrades_downgrades
    if df is None or len(df) == 0:
        return {}                                 # sin cobertura de analistas: no aplica
    rows = df.reset_index().to_dict("records")
    return parse_calificaciones(rows, date.today() - timedelta(days=dias))


def puntua_analistas(a: dict | None, f: dict | None) -> float:
    s = 0.0
    if a:
        s += 0.4 * (a["subidas"] - a["bajadas"]) + 0.15 * (a["obj_sube"] - a["obj_baja"])
        s = _clamp(s, -0.8, 0.8)
    if f:
        px = _num(f.get("currentPrice")) or _num(f.get("regularMarketPrice"))
        tgt, n = _num(f.get("targetMeanPrice")), _num(f.get("numberOfAnalystOpinions"), 0)
        if px and tgt and n >= 5:
            upside = (tgt / px - 1) * 100
            s += 0.2 if upside >= 25 else -0.3 if upside < -5 else 0.0
    return round(_clamp(s, -1.0, 1.0), 2)


def puntua_corto(f: dict | None) -> float:
    """Mucho corto = inversores informados apostando en contra (de media resta rentabilidad)."""
    sp = _num((f or {}).get("shortPercentOfFloat"))
    if sp is None:
        return 0.0
    sp = sp * 100 if sp <= 1 else sp
    return -0.4 if sp >= 25 else -0.2 if sp >= 15 else 0.0


def resumen_cadena(calls: list, puts: list, precio: float | None) -> dict:
    """calls/puts: dicts con strike, volume, openInterest, impliedVolatility."""
    v = lambda rows, k: sum(_num(r.get(k), 0.0) for r in rows)
    cv, pv = v(calls, "volume"), v(puts, "volume")
    oi = v(calls, "openInterest") + v(puts, "openInterest")
    iv = None
    if precio and calls and puts:
        c = min(calls, key=lambda r: abs(_num(r.get("strike"), 0) - precio))
        p = min(puts, key=lambda r: abs(_num(r.get("strike"), 0) - precio))
        ivs = [x for x in (_num(c.get("impliedVolatility")), _num(p.get("impliedVolatility"))) if x]
        iv = round(sum(ivs) / len(ivs) * 100, 1) if ivs else None
    return {"call_vol": int(cv), "put_vol": int(pv), "call_put": round(cv / pv, 2) if pv else None,
            "vol_oi": round((cv + pv) / oi, 2) if oi else None, "iv_atm_pct": iv}


@fuente("yahoo_opciones")
def opciones(ticker: str, precio: float | None = None) -> dict | None:
    if not YF_ENABLED:
        return None
    import yfinance as yf
    tk = yf.Ticker(ticker)
    exps = list(tk.options or [])
    if not exps:
        return {}                                 # sin opciones listadas: no aplica
    hoy = date.today()
    exp = next((e for e in exps if (date.fromisoformat(e) - hoy).days >= 3), exps[0])
    ch = tk.option_chain(exp)
    calls = ch.calls.to_dict("records")
    puts = ch.puts.to_dict("records")
    r = resumen_cadena(calls, puts, precio)
    r["vencimiento"] = exp
    return r


def puntua_opciones(o: dict | None) -> float:
    if not o or (o.get("call_vol", 0) + o.get("put_vol", 0)) < 500:
        return 0.0
    cp = o["call_put"] if o["call_put"] is not None else 99.0
    s = 0.5 if cp >= 2.5 else 0.25 if cp >= 1.6 else -0.4 if cp <= 0.6 else 0.0
    if s > 0 and (o.get("vol_oi") or 0) >= 1.5:
        s += 0.2                                   # actividad inusual (volumen > interés abierto)
    return round(s, 2)


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Sector: fuerza relativa del ETF sectorial frente al SPY (Alpaca)
# ═════════════════════════════════════════════════════════════════════════════
SECTOR_ETF = {"Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV", "Energy": "XLE",
              "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Industrials": "XLI",
              "Basic Materials": "XLB", "Real Estate": "XLRE", "Utilities": "XLU",
              "Communication Services": "XLC"}
ETFS = set(SECTOR_ETF.values()) | {"SPY", "QQQ", "IWM", "DIA", "SMH", "XBI", "ARKK", "TQQQ", "SOXL", "UPRO",
                                   "FNGU", "TECL", "LABU", "TNA", "SPXL", "UDOW", "NAIL"}
_bars_cache: dict = {}


def etf_por_sic(sic) -> str | None:
    s = int(_num(sic, 0) or 0)
    if not s:
        return None
    if s == 3674:
        return "SMH"
    if 7370 <= s <= 7379 or 3570 <= s <= 3579 or 3661 <= s <= 3679:
        return "XLK"
    if s in (6798,) or 6500 <= s <= 6553:
        return "XLRE"
    if 6000 <= s <= 6799:
        return "XLF"
    if 2833 <= s <= 2836 or 3841 <= s <= 3851 or 8000 <= s <= 8099:
        return "XLV"
    if 1300 <= s <= 1399 or s == 2911:
        return "XLE"
    if 4900 <= s <= 4999:
        return "XLU"
    if 4800 <= s <= 4899 or 7810 <= s <= 7819:
        return "XLC"
    if 2000 <= s <= 2199:
        return "XLP"
    if 5200 <= s <= 5999 or 3710 <= s <= 3716 or 7000 <= s <= 7099:
        return "XLY"
    if 1000 <= s <= 1499 or 2800 <= s <= 2829:
        return "XLB"
    if 1500 <= s <= 3999 or 4000 <= s <= 4799:
        return "XLI"
    return None


def etf_de(ticker: str, ficha: dict | None, sic=None) -> str | None:
    if ticker.upper() in ETFS:
        return None
    ind = str((ficha or {}).get("industry") or "")
    if "Semiconductor" in ind:
        return "SMH"
    return SECTOR_ETF.get(str((ficha or {}).get("sector") or "")) or etf_por_sic(sic)


def _closes(sym: str) -> list:
    if sym not in _bars_cache:
        start = (datetime.now(timezone.utc) - timedelta(days=50)).date().isoformat()
        h = {"APCA-API-KEY-ID": os.environ.get("APCA_KEY", ""), "APCA-API-SECRET-KEY": os.environ.get("APCA_SEC", "")}
        st, data = _get(f"https://data.alpaca.markets/v2/stocks/{sym}/bars?timeframe=1Day&start={start}"
                        f"&feed=iex&adjustment=all&limit=100", h)
        _bars_cache[sym] = [b["c"] for b in ((data or {}).get("bars") or [])]
    return _bars_cache[sym]


def fuerza_relativa(etf_c: list, spy_c: list) -> dict | None:
    if len(etf_c) < 21 or len(spy_c) < 21:
        return None
    r = lambda c, n: (c[-1] / c[-1 - n] - 1) * 100
    return {"rs5": round(r(etf_c, 5) - r(spy_c, 5), 2), "rs20": round(r(etf_c, 20) - r(spy_c, 20), 2)}


@fuente("sector_alpaca")
def sector(ticker: str, ficha: dict | None, sic=None) -> dict | None:
    etf = etf_de(ticker, ficha, sic)
    if not etf:
        return {}                                 # ETF o sector desconocido: no aplica
    fr = fuerza_relativa(_closes(etf), _closes("SPY"))
    if fr is None:
        raise RuntimeError(f"sin barras {etf}/SPY")
    return {"etf": etf, **fr}


def puntua_sector(s: dict | None) -> float:
    if not s or "rs20" not in s:
        return 0.0
    if s["rs20"] > 2 and s["rs5"] > 0:
        return 0.4
    if s["rs20"] < -3:
        return -0.4
    if s["rs20"] > 0:
        return 0.15
    if s["rs20"] < -1:
        return -0.15
    return 0.0


# ═════════════════════════════════════════════════════════════════════════════
# 6 · Social: ApeWisdom (menciones en Reddit)
# ═════════════════════════════════════════════════════════════════════════════
_ape_cache: dict = {}
_ape_lock = threading.Lock()


def _ape_tabla() -> dict:
    with _ape_lock:
        if "_ok" not in _ape_cache:
            for page in (1, 2, 3):
                st, data = _get(f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}",
                                {"User-Agent": UA, "Accept": "application/json"})
                for r in (data or {}).get("results") or []:
                    _ape_cache[str(r.get("ticker", "")).upper()] = r
                if not data:
                    break
            _ape_cache["_ok"] = len(_ape_cache) > 0
        if not _ape_cache["_ok"]:
            raise RuntimeError("ApeWisdom sin datos")
        return _ape_cache


@fuente("social_apewisdom")
def social(ticker: str) -> dict | None:
    r = _ape_tabla().get(ticker.upper())
    if not r:
        return {"en_ranking": False}
    m, m24 = _num(r.get("mentions"), 0), _num(r.get("mentions_24h_ago"), 0)
    return {"en_ranking": True, "rank": r.get("rank"), "menciones": int(m),
            "menciones_24h_antes": int(m24), "ratio_24h": round(m / m24, 2) if m24 else None}


def puntua_social(s: dict | None) -> float:
    if not s or not s.get("en_ranking"):
        return 0.0
    ratio = s.get("ratio_24h") or (3.0 if s["menciones"] >= 50 else 0)
    return 0.2 if s["menciones"] >= 30 and ratio >= 2 else 0.0


# ═════════════════════════════════════════════════════════════════════════════
# 7 · Macro: FRED + CNN Fear & Greed
# ═════════════════════════════════════════════════════════════════════════════
def parse_fred_csv(txt: str) -> list:
    """[(fecha, valor)] ignorando huecos ('.')."""
    rows = list(csv.reader(io.StringIO(txt)))
    out = []
    for r in rows[1:]:
        if len(r) >= 2 and r[1] not in ("", "."):
            v = _num(r[1])
            if v is not None:
                out.append((r[0], v))
    return out


@fuente("fred")
def fred_series(serie: str, dias: int = 120) -> list | None:
    desde = (date.today() - timedelta(days=dias)).isoformat()
    st, txt = _get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={serie}&cosd={desde}",
                   {"User-Agent": UA}, timeout=15, as_json=False)
    vals = parse_fred_csv(txt) if isinstance(txt, str) else []
    return vals or None


@fuente("cnn_fear_greed")
def fear_greed() -> dict | None:
    st, d = _get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                 {"User-Agent": UA, "Accept": "application/json", "Referer": "https://edition.cnn.com/",
                  "Origin": "https://edition.cnn.com"})
    fg = (d or {}).get("fear_and_greed") or {}
    sc = _num(fg.get("score"))
    return {"score": round(sc, 1), "rating": fg.get("rating")} if sc is not None else None


def regimen_macro(hy: list | None, nfci: list | None, curva: list | None, fg: dict | None) -> dict:
    """Ajuste común a todos los candidatos según el riesgo de mercado. Acotado a [-0.6, +0.2]."""
    out = {"hy_oas": None, "hy_cambio_20": None, "nfci": None, "curva_10a2a": None,
           "fear_greed": (fg or {}).get("score"), "regimen": "neutral", "ajuste": 0.0}
    aj = 0.0
    if hy and len(hy) >= 21:
        out["hy_oas"] = hy[-1][1]
        out["hy_cambio_20"] = round(hy[-1][1] - hy[-21][1], 2)
    if nfci:
        out["nfci"] = nfci[-1][1]
    if curva:
        out["curva_10a2a"] = curva[-1][1]
    ch, nf = out["hy_cambio_20"], out["nfci"]
    if (ch is not None and ch >= 0.5) or (nf is not None and nf > 0.25) or (out["hy_oas"] or 0) >= 6:
        out["regimen"], aj = "risk_off", -0.5
    elif (ch is not None and ch >= 0.25) or (nf is not None and nf > 0):
        out["regimen"], aj = "cautela", -0.2
    elif ch is not None and ch <= -0.2 and nf is not None and nf < -0.3:
        out["regimen"], aj = "risk_on", 0.1
    f = out["fear_greed"]
    if f is not None:
        aj += -0.2 if f < 20 else -0.1 if f > 85 else 0.0   # pánico o euforia extrema: más cautela
    out["ajuste"] = round(_clamp(aj, -0.6, 0.2), 2)
    return out


def macro_extra(presupuesto_s: float = 40) -> dict:
    """Nunca lanza ni tarda más de `presupuesto_s`. Si todo falla → régimen neutral, ajuste 0."""
    try:
        ex = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fxm")
        fs = [ex.submit(fred_series, "BAMLH0A0HYM2"), ex.submit(fred_series, "NFCI", 200),
              ex.submit(fred_series, "T10Y2Y"), ex.submit(fear_greed)]
        wait(fs, timeout=presupuesto_s)
        ex.shutdown(wait=False, cancel_futures=True)
        vals = [f.result() if f.done() else None for f in fs]
        return regimen_macro(*vals)
    except Exception as e:  # noqa: BLE001
        return {"regimen": "neutral", "ajuste": 0.0, "error": str(e)[:120]}


# ═════════════════════════════════════════════════════════════════════════════
# Enriquecimiento y ajuste
# ═════════════════════════════════════════════════════════════════════════════
def datos_ticker(ticker: str, precio: float | None = None, deadline: float | None = None) -> dict:
    f = ficha_yahoo(ticker)
    ins = insiders(ticker, deadline=deadline)
    px = precio or _num((f or {}).get("currentPrice")) or _num((f or {}).get("regularMarketPrice"))
    return {"ficha": f, "insiders": ins, "analistas": analistas(ticker), "opciones": opciones(ticker, px),
            "sector": sector(ticker, f, (ins or {}).get("sic")), "social": social(ticker)}


def enriquecer(tickers: list, precios: dict | None = None, presupuesto_s: float = PRESUPUESTO_S,
               hebras: int = 4) -> dict:
    """{ticker: datos}. Respeta el presupuesto: lo que no termina a tiempo se queda sin datos (ajuste 0)."""
    if not ACTIVADA or not tickers:
        return {}
    tickers = list(dict.fromkeys(t.upper() for t in tickers))[:MAX_TICKERS]
    deadline = time.time() + presupuesto_s
    ex = ThreadPoolExecutor(max_workers=hebras, thread_name_prefix="fx")
    futs = {ex.submit(datos_ticker, t, (precios or {}).get(t), deadline - 10): t for t in tickers}
    done, pending = wait(futs, timeout=presupuesto_s)
    ex.shutdown(wait=False, cancel_futures=True)
    out = {}
    for fu in done:
        try:
            out[futs[fu]] = fu.result()
        except Exception:  # noqa: BLE001
            pass
    if pending:
        _marca("presupuesto", False, f"sin tiempo para {sorted(futs[p] for p in pending)}", 0)
    return out


def ajuste(d: dict | None, macro: dict | None = None) -> tuple:
    """(ajuste_total, partes). Total acotado a ±AJUSTE_MAX."""
    d = d or {}
    partes = {"insiders": puntua_insiders(d.get("insiders")),
              "analistas": puntua_analistas(d.get("analistas"), d.get("ficha")),
              "opciones": puntua_opciones(d.get("opciones")),
              "corto": puntua_corto(d.get("ficha")),
              "sector": puntua_sector(d.get("sector")),
              "social": puntua_social(d.get("social")),
              "macro": float((macro or {}).get("ajuste") or 0.0)}
    partes = {k: round(v, 2) for k, v in partes.items() if v}
    return round(_clamp(sum(partes.values()), -AJUSTE_MAX, AJUSTE_MAX), 2), partes


def scores_por_cuenta(base: float, aj: float) -> tuple:
    """Paper usa el ajuste completo; real solo la parte negativa (las fuentes nuevas aún no están validadas)."""
    return round(_clamp(base + aj, 0.0, 10.0), 2), round(_clamp(base + min(0.0, aj), 0.0, 10.0), 2)


# ── Salud (para fuentes_estado del log) ──────────────────────────────────────
def health() -> dict:
    """{fuente: (ok, detalle)} con AAPL. Nunca lanza; ~10-20 s."""
    res = {}

    def chk(nombre, fn):
        try:
            ok, det = fn()
        except Exception as e:  # noqa: BLE001
            ok, det = False, f"{type(e).__name__}: {e}"[:100]
        res[nombre] = (bool(ok), det)

    def _sec():
        i = insiders("AAPL", dias=60)
        return i is not None, (f"{i['form4_leidos']} Form 4 leídos" if i else
                               (ESTADO.get("sec_insiders") or {}).get("ultimo_error") or "sin datos")
    chk("sec_edgar", _sec)
    chk("fred", lambda: (lambda v: (bool(v), f"HY OAS {v[-1][1]}" if v else "sin datos"))(fred_series("BAMLH0A0HYM2")))
    chk("cnn_fear_greed", lambda: (lambda v: (bool(v), f"{v}" if v else "sin datos"))(fear_greed()))
    chk("apewisdom", lambda: (lambda v: (v is not None, f"{v}" if v else "sin datos"))(social("NVDA")))
    if YF_ENABLED:
        chk("yahoo_analistas", lambda: (lambda v: (v is not None, f"{v and {k: v[k] for k in ('subidas', 'bajadas')}}"))(analistas("AAPL", 90)))
        chk("yahoo_opciones", lambda: (lambda v: (v is not None, f"C/P {v and v['call_put']}"))(opciones("AAPL")))
        chk("yahoo_ficha", lambda: (lambda v: (v is not None, f"sector {v and v.get('sector')}"))(ficha_yahoo("AAPL")))
    chk("sector_alpaca", lambda: (lambda v: (v is not None, f"{v}"))(sector("AAPL", {"sector": "Technology"})))
    return res


if __name__ == "__main__":
    import sys
    tks = [a.upper() for a in sys.argv[1:]] or ["AAPL", "NVDA", "JPM"]
    for k, (ok, det) in health().items():
        print(f"{'✅' if ok else '❌'} {k}: {det}")
    m = macro_extra()
    print("macro:", json.dumps(m, ensure_ascii=False))
    datos = enriquecer(tks, presupuesto_s=120)
    for t in tks:
        aj, partes = ajuste(datos.get(t), m)
        d = datos.get(t) or {}
        print(f"\n{t}: ajuste {aj:+.2f} {partes}")
        for k in ("insiders", "analistas", "opciones", "sector", "social"):
            print(f"   {k}: {json.dumps(d.get(k), ensure_ascii=False, default=str)[:220]}")
    print("\nestado:", json.dumps(resumen_estado(), ensure_ascii=False))
