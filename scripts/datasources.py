#!/usr/bin/env python3
"""
datasources.py — capa única de acceso a TODAS las fuentes de datos (v1, 2026-09-10).

Una sola implementación, con reintentos y verificación, para que ninguna tarea
vuelva a "romper" el acceso a una fuente. Fuentes:

  EarningsWhispers  API JSON pública (sin login). CLAVE: requiere la cabecera
                    Referer: https://www.earningswhispers.com/ — sin ella el
                    servidor responde HTTP 204 vacío (causa de los "EW caído"
                    de agosto-septiembre). old.earningswhispers.com está muerto
                    y www.* es una SPA: NUNCA scrapear HTML.
                      /api/caldata/{YYYYMMDD}           calendario del día
                      /api/quickcaldata/{YYYYMMDD}/{1|3} calendario BMO / AMC
                      /api/getstocksdata/{TICKER}       consenso, whisper, fecha
                      /api/todaysresults                resultados publicados hoy
  Nasdaq            api.nasdaq.com/api/calendar/earnings (respaldo del calendario)
  Alpaca            precios (snapshots/bars IEX), noticias (v1beta1/news), calendario
  Yahoo (yfinance)  historial de sorpresas EPS → beat rate y reacción del precio,
                    revisiones de analistas, VIX
Uso:  from datasources import ew_calendar, ew_stock, health_check, ...
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
EW_BASE = "https://www.earningswhispers.com"
EW_HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
              "Referer": "https://www.earningswhispers.com/", "X-Requested-With": "XMLHttpRequest"}
NASDAQ_HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                  "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
YF_ENABLED = os.environ.get("NO_YF", "0") != "1"


def _get_json(url: str, headers: dict, timeout: int = 20, retries: int = 3):
    """GET JSON con reintentos. Devuelve (status, data|None)."""
    last = (0, None)
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                body = r.read()
                if r.status == 204 or not body.strip():
                    return 204, None
                return r.status, json.loads(body)
        except urllib.error.HTTPError as e:
            last = (e.code, None)
            if e.code not in (429, 500, 502, 503, 504):
                return last
        except Exception:
            last = (0, None)
        time.sleep(1.5 * (i + 1))
    return last


# ── EarningsWhispers ─────────────────────────────────────────────────────────
def ew_get(path: str):
    return _get_json(f"{EW_BASE}/{path.lstrip('/')}", EW_HEADERS)


def ew_calendar(d: date) -> list:
    """Empresas que reportan el día d. Cada item: ticker, releaseTime (1=BMO, 3=AMC), total (nº analistas)…"""
    ds = d.strftime("%Y%m%d")
    st, data = ew_get(f"api/caldata/{ds}")
    rows = data if isinstance(data, list) else []
    if not rows:  # respaldo: BMO + AMC por separado ('/all' da 500)
        for rt in (1, 3):
            _, part = ew_get(f"api/quickcaldata/{ds}/{rt}")
            rows += part if isinstance(part, list) else []
    return rows


def ew_stock(ticker: str) -> dict | None:
    """consensusEst, whisper (999 = sin whisper), nextEPSDate, releaseTime, avgEPSMove, sectName…"""
    st, data = ew_get(f"api/getstocksdata/{ticker.upper()}")
    if not isinstance(data, dict):
        return None
    if data.get("whisper") in (999, 999.0):
        data["whisper"] = None
    return data


def ew_results_today() -> list:
    st, data = ew_get("api/todaysresults")
    return data if isinstance(data, list) else []


# ── Nasdaq (respaldo de calendario) ─────────────────────────────────────────
def nasdaq_calendar(d: date) -> list:
    st, data = _get_json(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}", NASDAQ_HEADERS)
    rows = ((data or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        t = (r.get("time") or "")
        out.append({"ticker": r.get("symbol"), "company": r.get("name"),
                    "releaseTime": 1 if "pre" in t else 3 if "after" in t else None,
                    "total": int(r.get("noOfEsts") or 0) if str(r.get("noOfEsts") or "").isdigit() else 0,
                    "epsForecast": r.get("epsForecast"), "marketCap": r.get("marketCap"), "fuente": "nasdaq"})
    return out


def earnings_calendar(d: date) -> list:
    """Calendario combinado EW + Nasdaq (sin duplicados). EW manda si hay conflicto."""
    ew = ew_calendar(d)
    seen = {r["ticker"] for r in ew if r.get("ticker")}
    for r in ew:
        r["fuente"] = "ew"
    extra = [r for r in nasdaq_calendar(d) if r.get("ticker") and r["ticker"] not in seen]
    return ew + extra


# ── Alpaca (datos) ───────────────────────────────────────────────────────────
def _alp_headers():
    return {"APCA-API-KEY-ID": os.environ.get("APCA_KEY", ""), "APCA-API-SECRET-KEY": os.environ.get("APCA_SEC", "")}


def alpaca_snapshots(tickers: list) -> dict:
    out = {}
    for i in range(0, len(tickers), 100):
        chunk = ",".join(tickers[i:i + 100])
        st, data = _get_json(f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={chunk}&feed=iex", _alp_headers())
        out.update(data or {})
    return out


def alpaca_daily_bars(ticker: str, days: int = 40) -> list:
    start = (datetime.now(timezone.utc) - timedelta(days=days * 1.6)).date().isoformat()
    st, data = _get_json(f"https://data.alpaca.markets/v2/stocks/{ticker}/bars?timeframe=1Day&start={start}"
                         f"&feed=iex&adjustment=all&limit=1000", _alp_headers())
    return (data or {}).get("bars") or []


def alpaca_news_count(ticker: str, days: int = 3) -> int:
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    st, data = _get_json(f"https://data.alpaca.markets/v1beta1/news?symbols={ticker}&start={start}&limit=50",
                         _alp_headers())
    return len((data or {}).get("news") or [])


# ── Yahoo (yfinance) ─────────────────────────────────────────────────────────
def yf_earnings_profile(ticker: str, n: int = 8) -> dict:
    """Beat rate, sorpresa media y reacción media del precio al día siguiente en los últimos n trimestres."""
    out = {"beat_rate": None, "n": 0, "avg_surprise_pct": None, "avg_reaction_pct": None,
           "rev_up_30d": None, "rev_down_30d": None, "error": None}
    if not YF_ENABLED:
        out["error"] = "yfinance desactivado"; return out
    try:
        import pandas as pd
        import yfinance as yf
        t = yf.Ticker(ticker)
        ed = t.get_earnings_dates(limit=20)
        if ed is not None and len(ed):
            ed = ed.dropna(subset=["Reported EPS", "EPS Estimate"]).head(n)
            hist = t.history(period="3y", timeout=15)
            if len(hist):
                hist.index = hist.index.tz_localize(None)
            beats, surp, reac = 0, [], []
            for dt, row in ed.iterrows():
                rep, est = float(row["Reported EPS"]), float(row["EPS Estimate"])
                beats += rep > est
                if abs(est) > 1e-9:
                    surp.append((rep - est) / abs(est) * 100)
                if len(hist):
                    ts = pd.Timestamp(dt)
                    if ts.tzinfo is not None:
                        ts = ts.tz_convert("America/New_York").tz_localize(None)
                    d0, amc = ts.normalize(), ts.hour >= 12
                    before = hist[hist.index <= d0] if amc else hist[hist.index < d0]
                    after = hist[hist.index > d0] if amc else hist[hist.index >= d0]
                    if len(before) and len(after):
                        reac.append((float(after["Close"].iloc[0]) / float(before["Close"].iloc[-1]) - 1) * 100)
            out["n"] = len(ed)
            if len(ed):
                out["beat_rate"] = round(100 * beats / len(ed), 1)
            out["avg_surprise_pct"] = round(sum(surp) / len(surp), 2) if surp else None
            out["avg_reaction_pct"] = round(sum(reac) / len(reac), 2) if reac else None
        try:
            rv = t.eps_revisions
            if rv is not None and len(rv) and "upLast30days" in rv.columns:
                out["rev_up_30d"] = int(rv["upLast30days"].fillna(0).iloc[0])
                out["rev_down_30d"] = int(rv["downLast30days"].fillna(0).iloc[0])
        except Exception:
            pass
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


def macro_state() -> dict:
    """VIX y tendencia del SPY (precio vs media 50 sesiones)."""
    out = {"vix": None, "spy_above_ma50": None}
    bars = alpaca_daily_bars("SPY", 60)
    if len(bars) >= 50:
        closes = [b["c"] for b in bars]
        out["spy_above_ma50"] = closes[-1] > sum(closes[-50:]) / 50
    if YF_ENABLED:
        try:
            import yfinance as yf
            h = yf.Ticker("^VIX").history(period="5d", timeout=10)
            if len(h):
                out["vix"] = round(float(h["Close"].iloc[-1]), 2)
        except Exception:
            pass
    return out


# ── Salud de las fuentes ─────────────────────────────────────────────────────
def health_check() -> dict:
    """Comprueba cada fuente con una petición real. {fuente: (ok, detalle)}"""
    res = {}
    st, d = ew_get("api/getstocksdata/AAPL")
    res["earningswhispers"] = (isinstance(d, dict) and d.get("ticker") == "AAPL", f"HTTP {st}")
    today = datetime.now(timezone.utc).date()
    cal = []
    for k in range(0, 7):
        cal = nasdaq_calendar(today + timedelta(days=k))
        if cal:
            break
    res["nasdaq_calendar"] = (bool(cal), f"{len(cal)} filas")
    snap = alpaca_snapshots(["SPY"])
    res["alpaca_datos"] = ("SPY" in snap, "snapshot SPY")
    st, news = _get_json("https://data.alpaca.markets/v1beta1/news?symbols=SPY&limit=1", _alp_headers())
    res["alpaca_noticias"] = (isinstance(news, dict) and "news" in news, f"HTTP {st}")
    if YF_ENABLED:
        prof = yf_earnings_profile("AAPL", 4)
        res["yahoo_finance"] = (prof.get("beat_rate") is not None, prof.get("error") or f"beat_rate AAPL {prof.get('beat_rate')}")
    return res
