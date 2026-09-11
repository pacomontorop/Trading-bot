#!/usr/bin/env python3
"""
common.py — núcleo compartido de los scripts de GitHub Actions.

Qué centraliza:
  • Clientes Alpaca (paper / real) con reintentos y modo DRY_RUN.
  • Lectura/escritura de performance_log.json en GitHub con reintento ante
    conflictos de SHA (varias tareas Cowork + GHA escriben el mismo fichero)
    y soporte para ficheros > 1 MB.
  • Límites duros (config/risk_limits.json) combinados con los parámetros del
    log, aplicando SIEMPRE el valor más estricto.
  • Sizing por riesgo (R), niveles stop/TP por ATR y R:R mínimo.
  • Ventana horaria basada en el calendario oficial de Alpaca (robusto a
    cambios de horario de verano y a retrasos del cron de GitHub).

Variables de entorno:
  APCA_KEY / APCA_SEC           cuenta paper (obligatorias)
  APCA_KEY_R / APCA_SEC_R       cuenta real (opcionales; sin ellas no se opera real)
  GH_TOKEN                      token con permiso contents:write en el repo
  TG_TOKEN / TG_CHAT            Telegram (opcional)
  DRY_RUN=1                     no envía órdenes, no escribe log, no manda Telegram
  LOCAL_LOG=/ruta.json          lee (y en no-dry-run escribe) el log local en vez de GitHub
  FORCE_WINDOW=1                ignora la ventana horaria (solo pruebas)
"""
from __future__ import annotations

import base64
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
LIMITS_PATH = ROOT / "config" / "risk_limits.json"

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
LOCAL_LOG = os.environ.get("LOCAL_LOG", "")
FORCE_WINDOW = os.environ.get("FORCE_WINDOW", "0") == "1"
YF_ENABLED = os.environ.get("NO_YF", "0") != "1"   # NO_YF=1 → sin yfinance (pruebas / Yahoo caído)

GH_REPO = os.environ.get("GH_REPO", "pacomontorop/Trading-bot")
GH_PATH = os.environ.get("GH_LOG_PATH", "performance_log.json")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")

PAPER_BASE = "https://paper-api.alpaca.markets/v2"
REAL_BASE = "https://api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets/v2"

OPEN_ORDER_STATES = {"new", "accepted", "held", "partially_filled", "pending_new",
                     "accepted_for_bidding", "pending_replace", "replaced"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


# ── HTTP ─────────────────────────────────────────────────────────────────────
def http(url: str, method: str = "GET", headers: dict | None = None, data=None,
         timeout: int = 20, retries: int = 3, raw: bool = False):
    """Devuelve (status, body). Reintenta 429/5xx/errores de red con backoff."""
    body = None
    if data is not None:
        body = data if isinstance(data, (bytes, bytearray)) else json.dumps(data).encode()
    h = dict(headers or {})
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    last = (0, {"_error": "sin respuesta"})
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read()
                if raw:
                    return r.status, txt
                return r.status, (json.loads(txt) if txt.strip() else {"_ok": True})
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")[:500]
            last = (e.code, {"_error": txt, "_code": e.code})
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return last
        except Exception as e:  # red / timeout
            last = (0, {"_error": str(e)})
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
    return last


# ── Telegram ─────────────────────────────────────────────────────────────────
def tg(msg: str) -> None:
    log("[TG] " + msg.replace("\n", "\n[TG] "))
    if DRY_RUN or not (TG_TOKEN and TG_CHAT):
        return
    http(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", "POST",
         data={"chat_id": TG_CHAT, "text": msg[:4000]}, timeout=10, retries=2)


# ── Alpaca ───────────────────────────────────────────────────────────────────
class Alpaca:
    def __init__(self, key: str, sec: str, base: str, name: str):
        self.name, self.base = name, base
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}

    def req(self, path: str, method: str = "GET", data=None):
        if DRY_RUN and method != "GET":
            log(f"  [DRY_RUN {self.name}] {method} {path} {json.dumps(data) if data else ''}")
            return {"_dry": True, "id": f"dry-{int(time.time()*1000)}", "status": "dry"}
        _, body = http(self.base + path, method, self.h, data)
        return body

    def data(self, path: str):
        _, body = http(DATA_BASE + path, "GET", self.h)
        return body

    # atajos
    def account(self):
        return self.req("/account")

    def positions(self) -> list:
        r = self.req("/positions")
        return r if isinstance(r, list) else []

    def clock(self):
        return self.req("/clock")

    def open_orders(self, symbols: str | None = None) -> list:
        """Órdenes abiertas aplanadas (incluye patas de brackets, p.ej. stop 'held')."""
        q = "/orders?status=open&nested=true&limit=500"
        if symbols:
            q += f"&symbols={symbols}"
        r = self.req(q)
        out = []
        for o in (r if isinstance(r, list) else []):
            out.append(o)
            for leg in (o.get("legs") or []):
                if leg.get("status") in OPEN_ORDER_STATES:
                    out.append(leg)
        return out

    def orders_since(self, after_iso: str) -> list:
        r = self.req(f"/orders?status=all&limit=500&after={after_iso}")
        return r if isinstance(r, list) else []

    def latest_price(self, sym: str) -> float | None:
        r = self.data(f"/stocks/{sym}/trades/latest?feed=iex")
        try:
            return float(r["trade"]["p"])
        except Exception:
            return None


def paper_client() -> Alpaca:
    return Alpaca(os.environ["APCA_KEY"], os.environ["APCA_SEC"], PAPER_BASE, "PAPER")


def real_client() -> Alpaca | None:
    k, s = os.environ.get("APCA_KEY_R", ""), os.environ.get("APCA_SEC_R", "")
    return Alpaca(k, s, REAL_BASE, "REAL") if k and s else None


def is_crypto(pos: dict) -> bool:
    return pos.get("asset_class") == "crypto" or "/" in pos.get("symbol", "")


def day_pnl_pct(acct: dict) -> float:
    try:
        eq, last = float(acct["equity"]), float(acct["last_equity"])
        return (eq / last - 1) * 100 if last else 0.0
    except Exception:
        return 0.0


# ── Calendario / ventana horaria ─────────────────────────────────────────────
def session_today(alp: Alpaca):
    """(apertura, cierre) de hoy en ET según el calendario de Alpaca, o None si no hay sesión."""
    d = now_utc().astimezone(ET).date().isoformat()
    cal = alp.req(f"/calendar?start={d}&end={d}")
    if not isinstance(cal, list) or not cal or cal[0].get("date") != d:
        return None
    o, c = cal[0]["open"], cal[0]["close"]
    mk = lambda hm: datetime.fromisoformat(f"{d}T{hm}:00").replace(tzinfo=ET)
    return mk(o), mk(c)


def minutes_since_open(alp: Alpaca) -> float | None:
    s = session_today(alp)
    if not s:
        return None
    return (now_utc() - s[0].astimezone(timezone.utc)).total_seconds() / 60


def et_today() -> str:
    return now_utc().astimezone(ET).date().isoformat()


# ── performance_log.json en GitHub ───────────────────────────────────────────
def _gh_headers(accept="application/vnd.github+json"):
    return {"Authorization": f"Bearer {GH_TOKEN}", "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28"}


def read_log():
    """Devuelve (plog, sha). Soporta ficheros > 1 MB (la API 'contents' no trae el contenido)."""
    if LOCAL_LOG:
        return json.loads(Path(LOCAL_LOG).read_text(encoding="utf-8")), None
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
    st, meta = http(url, headers=_gh_headers())
    if st != 200:
        raise RuntimeError(f"read_log {st}: {meta}")
    sha = meta["sha"]
    content = meta.get("content") or ""
    if content.strip():
        txt = base64.b64decode(content)
    else:  # > 1 MB
        st, txt = http(url, headers=_gh_headers("application/vnd.github.raw"), raw=True, timeout=60)
        if st != 200:
            raise RuntimeError(f"read_log raw {st}")
    return json.loads(txt), sha


def update_log(mutate, message: str, attempts: int = 5):
    """Lee el log, aplica mutate(plog) y lo escribe. Si otro proceso escribió entre
    medias (409/422 por SHA), vuelve a leer y re-aplica mutate. mutate debe ser idempotente."""
    for i in range(attempts):
        plog, sha = read_log()
        mutate(plog)
        if DRY_RUN:
            log(f"  [DRY_RUN] log no escrito: {message}")
            return plog
        if LOCAL_LOG:
            Path(LOCAL_LOG).write_text(json.dumps(plog, ensure_ascii=False, indent=2), encoding="utf-8")
            return plog
        body = {"message": message,
                "content": base64.b64encode(json.dumps(plog, ensure_ascii=False, indent=2).encode()).decode(),
                "sha": sha}
        st, resp = http(f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}", "PUT",
                        _gh_headers(), body, timeout=60, retries=1)
        if st in (200, 201):
            return plog
        if st in (409, 422):
            log(f"  conflicto SHA al escribir log (intento {i+1}) — releyendo")
            time.sleep(2 + 2 * i)
            continue
        raise RuntimeError(f"update_log {st}: {resp}")
    raise RuntimeError("update_log: demasiados conflictos de SHA")


# ── Límites efectivos ────────────────────────────────────────────────────────
def load_limits() -> dict:
    return json.loads(LIMITS_PATH.read_text(encoding="utf-8"))


def _f(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


def effective_limits(plog: dict, limits: dict) -> dict:
    """Combina límites duros y parámetros del log quedándose con lo MÁS ESTRICTO."""
    p = plog.get("parametros_activos", {}) or {}
    L = json.loads(json.dumps(limits))  # copia
    paper, real = L["paper"], L["real"]

    if not paper.get("ignore_log_limits"):
        paper["min_score"] = max(paper["min_score"], _f(p, "score_min_paper", "min_signal_score", default=0))
    real["min_score"] = max(real["min_score"],
                            _f(p, "score_threshold_real_account", default=0),
                            _f(p, "score_min_real", default=0))
    if not paper.get("ignore_log_limits"):
        paper["max_positions"] = int(min(paper["max_positions"], _f(p, "max_cowork_positions", default=99)))
    real["max_positions"] = int(min(real["max_positions"], _f(p, "max_real_positions", default=99)))

    hard = real.get("enabled", False)
    log_flag = p.get("real_trading_enabled", True)  # el log solo puede APAGAR, nunca encender
    gate = ((plog.get("kpis") or {}).get("rendimiento_verificado") or {}).get("gate_real_ok") is True
    if hard is False or log_flag is False:
        real["active"], real["why"] = False, "desactivado (risk_limits o log)"
    elif hard == "auto":
        real["active"] = gate
        real["why"] = "gate paper OK" if gate else "auto: paper aún no demuestra edge (gate_real_ok=false)"
    else:
        real["active"], real["why"] = True, "forzado ON en risk_limits.json"
    L["leveraged"] = set(s.upper() for s in L.get("leveraged_etfs", []))
    return L


# ── Precios, niveles y sizing ────────────────────────────────────────────────
def round_px(p: float) -> float:
    return round(p, 2) if p >= 1 else round(p, 4)


def atr14(sym: str, alp: Alpaca | None = None) -> float | None:
    """ATR(14) diario. yfinance (consolidado) y, si falla, barras IEX de Alpaca."""
    try:
        if not YF_ENABLED:
            raise RuntimeError("yfinance desactivado")
        import yfinance as yf
        h = yf.Ticker(sym).history(period="2mo", interval="1d", timeout=10)
        if len(h) >= 15:
            hi, lo, cl = h["High"], h["Low"], h["Close"]
            trs = [max(hi.iloc[i] - lo.iloc[i], abs(hi.iloc[i] - cl.iloc[i-1]), abs(lo.iloc[i] - cl.iloc[i-1]))
                   for i in range(len(h) - 14, len(h))]
            return float(sum(trs) / 14)
    except Exception:
        pass
    if alp:
        start = (now_utc() - timedelta(days=40)).date().isoformat()
        r = alp.data(f"/stocks/{sym}/bars?timeframe=1Day&start={start}&feed=iex&limit=100")
        bars = (r or {}).get("bars") or []
        if len(bars) >= 15:
            trs = [max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - bars[i-1]["c"]),
                       abs(bars[i]["l"] - bars[i-1]["c"])) for i in range(len(bars) - 14, len(bars))]
            return float(sum(trs) / 14)
    return None


def compute_levels(price: float, cand_stop_pct: float | None, atr: float | None, ex: dict) -> dict:
    """Stop = max(stop del candidato, 1.5×ATR) acotado a [min,max]%; TP = entrada + min_rr × R."""
    stop_pct = (cand_stop_pct * 100) if cand_stop_pct else 5.0
    if atr and price:
        stop_pct = max(stop_pct, ex["atr_stop_mult"] * atr / price * 100)
    stop_pct = min(max(stop_pct, ex["stop_pct_min"]), ex["stop_pct_max"])
    stop = price * (1 - stop_pct / 100)
    risk = price - stop
    tp = price + ex["min_rr"] * risk
    return {"stop": round_px(stop), "stop_limit": round_px(stop * 0.995), "tp": round_px(tp),
            "stop_pct": round(stop_pct, 2), "risk_per_share": risk, "rr": ex["min_rr"]}


def size_by_risk(equity: float, price: float, risk_per_share: float, risk_pct: float,
                 max_pos_pct: float, buying_power: float) -> int:
    """Nº de acciones tal que la pérdida hasta el stop = risk_pct% del equity, con topes."""
    if price <= 0 or risk_per_share <= 0:
        return 0
    by_risk = math.floor(equity * risk_pct / 100 / risk_per_share)
    by_cap = math.floor(equity * max_pos_pct / 100 / price)
    by_bp = math.floor(buying_power * 0.95 / price)
    return max(0, min(by_risk, by_cap, by_bp))


def place_bracket_entry(alp: Alpaca, sym: str, qty: int, limit_px: float, lv: dict, cid: str) -> dict:
    return alp.req("/orders", "POST", {
        "symbol": sym, "qty": str(qty), "side": "buy", "type": "limit",
        "limit_price": str(round_px(limit_px)), "time_in_force": "gtc",
        "order_class": "bracket", "client_order_id": cid[:48],
        "take_profit": {"limit_price": str(lv["tp"])},
        "stop_loss": {"stop_price": str(lv["stop"]), "limit_price": str(lv["stop_limit"])},
    })


def wait_fill_or_cancel(alp: Alpaca, order_id: str, timeout_s: int) -> dict:
    """Espera el fill; si no llena a tiempo cancela (cancelar el padre cancela las patas)."""
    if DRY_RUN or not order_id or str(order_id).startswith("dry-"):
        return {"status": "dry", "filled_qty": "0"}
    t0 = time.time()
    o = {}
    while time.time() - t0 < timeout_s:
        o = alp.req(f"/orders/{order_id}")
        if o.get("status") in ("filled", "canceled", "rejected", "expired"):
            return o
        time.sleep(5)
    alp.req(f"/orders/{order_id}", "DELETE")
    time.sleep(2)
    o = alp.req(f"/orders/{order_id}")
    o["_timeout_cancel"] = True
    return o


def count_new_entries_today(alp: Alpaca, prefix: str) -> int:
    s = session_today(alp)
    start = (s[0] if s else now_utc().astimezone(ET).replace(hour=0, minute=0)).astimezone(timezone.utc)
    n = 0
    for o in alp.orders_since(start.strftime("%Y-%m-%dT%H:%M:%SZ")):
        if o.get("side") != "buy" or not (o.get("client_order_id") or "").startswith(prefix):
            continue
        if float(o.get("filled_qty") or 0) > 0 or o.get("status") in OPEN_ORDER_STATES:
            n += 1
    return n


# ── Guardrails sobre parametros_activos (los leen las tareas Cowork) ─────────
GUARDRAILS_FLAG = "_hardening_2026_09_10"


def enforce_guardrails(pl: dict) -> list:
    """Re-aplica mínimos de seguridad en performance_log.parametros_activos para que
    ninguna tarea los relaje. Devuelve la lista de cambios hechos. Idempotente."""
    p = pl.setdefault("parametros_activos", {})
    lim = load_limits()
    cambios = []

    def floor(k, v):
        try:
            cur = float(p.get(k))
        except (TypeError, ValueError):
            cur = None
        if cur is None or cur < v:
            cambios.append(f"{k}: {p.get(k)} → {v}"); p[k] = v

    def setv(k, v):
        if p.get(k) != v:
            cambios.append(f"{k}: {p.get(k)} → {v}"); p[k] = v

    # Seguridad (siempre)
    floor("score_threshold_real_account", lim["real"]["min_score"])
    floor("score_min_real", lim["real"]["min_score"])
    floor("r_ratio_minimo", lim["execution"]["min_rr"])
    setv("bloqueo_etf_apalancado", True)
    if "real_trading_enabled" not in p:
        setv("real_trading_enabled", True)  # el log solo puede APAGAR; risk_limits.json manda

    # Gestión (una sola vez; después Cowork puede recalibrar con evidencia)
    if not p.get(GUARDRAILS_FLAG):
        floor("be_lock_R_threshold_ew", lim["management"]["breakeven_at_r"])
        floor("breakeven_stop_at_r", lim["management"]["breakeven_at_r"])
        floor("trailing_stop_activate_at_r", lim["management"]["breakeven_at_r"])
        tp = p.get("toma_parciales")
        if isinstance(tp, dict) and tp.get("activado"):
            tp["activado"] = False
            tp["motivo_desactivado"] = ("2026-09-10: 133 cierres paper (abr-sep) → win rate 62% pero ganancia media "
                                        "$129 vs pérdida media $241 (PF 0.86). Parciales y break-even temprano recortan "
                                        "ganadoras. Se deja correr hasta TP=2R con stop a BE en +1R y lock +1R en +2R.")
            cambios.append("toma_parciales.activado: True → False")
        p["_nota_threshold"] = ("Umbral real = max(parametros, config/risk_limits.json). Cuenta real solo con candidatos "
                                "EW/Cowork, sin ETFs apalancados y con gate de rendimiento paper (kpi_report.py).")
        p[GUARDRAILS_FLAG] = {"aplicado": now_utc().strftime("%Y-%m-%dT%H:%MZ"), "cambios": list(cambios)}
    return cambios
