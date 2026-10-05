#!/usr/bin/env python3
"""
EW Archivo - registrador puramente observacional (2026-09-28).

POR QUE EXISTE
--------------
Todo el sistema se construyo sobre una hipotesis: que la asimetria entre el whisper de
EarningsWhispers y el consenso predice la reaccion del valor. Esa hipotesis NUNCA se ha
podido contrastar, por dos razones:

  1. La API de EW solo expone el trimestre EN CURSO. api/caldata/<fecha pasada> devuelve
     vacio y no trae el campo whisper en ningun caso. No existe serie historica accesible
     por la via permitida (y scrapear o hacer login esta prohibido).
  2. Nadie la ha ido guardando. A 2026-09-28, despues de semanas operando, el
     performance_log contiene CUATRO observaciones utilizables con whisper + consenso +
     reaccion. Cuatro. Para detectar un efecto de 2 puntos porcentuales sobre una reaccion
     con desviacion tipica de ~6 puntos hacen falta del orden de 140 observaciones.

Este script no opera, no puntua, no decide nada y no toca config/risk_limits.json.
Solo mira y apunta, todos los dias, pase lo que pase. En un año habra una muestra con la
que se podra responder a la pregunta. Hoy no la hay, y mientras no la haya cualquier
afirmacion sobre el whisper es una opinion, no un resultado.

DISEÑO: SOLO SE AÑADE, NUNCA SE CORRIGE
---------------------------------------
data/ew_archivo.jsonl, una linea por observacion, dos tipos:

  {"tipo":"captura",    ...}  snapshot ANTES del resultado: whisper, consenso, analistas,
                              precio, hora de publicacion. Con capturado_utc para poder
                              demostrar despues que se escribio antes del evento.
  {"tipo":"resolucion", ...}  DESPUES: eps real, sorpresa, y reaccion a 1/3/5 sesiones.

Nunca se reescribe una linea. Si un dato llega corregido se añade una linea nueva. Esto es
deliberado: un archivo que se puede editar a posteriori no sirve para contrastar nada,
porque no hay forma de distinguir un dato de una racionalizacion. El sesgo de anticipacion
(look-ahead) se cuela justo por ahi - me paso a mi mismo en este proyecto, seleccionando
valores por una liquidez medida durante el propio periodo de prueba.
"""
import base64
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

from common import DRY_RUN, GH_REPO, _gh_headers, http, log, now_utc, paper_client
from datasources import alpaca_daily_bars, ew_calendar, ew_results_today, ew_stock

RUTA = os.environ.get("EW_ARCHIVO_PATH", "data/ew_archivo.jsonl")
LOCAL = os.environ.get("EW_ARCHIVO_LOCAL", "")   # ruta en disco: no toca GitHub
DIAS_VISTA = 3          # cuantas sesiones por delante se capturan
DIAS_RESOLVER = 12      # ventana hacia atras para resolver capturas pendientes
MAX_TICKERS = 60        # tope de llamadas a ew_stock por ejecucion


# -- lectura / escritura del archivo -----------------------------------------
def leer_archivo():
    """-> (lineas, sha). Fichero inexistente -> ([], None)."""
    if LOCAL:
        try:
            txt = open(LOCAL, encoding="utf-8").read()
        except FileNotFoundError:
            return [], None
        return _parsear(txt), None
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{RUTA}"
    st, meta = http(url, headers=_gh_headers())
    if st == 404:
        return [], None
    if st != 200:
        raise RuntimeError(f"leer_archivo {st}: {meta}")
    sha = meta["sha"]
    content = meta.get("content") or ""
    if content.strip():
        txt = base64.b64decode(content).decode("utf-8", "replace")
    else:                                     # > 1 MB: la API no trae el contenido
        st, raw = http(url, headers=_gh_headers("application/vnd.github.raw"),
                       raw=True, timeout=60)
        if st != 200:
            raise RuntimeError(f"leer_archivo raw {st}")
        txt = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    return _parsear(txt), sha


def _parsear(txt):
    filas = []
    for ln in txt.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            filas.append(json.loads(ln))
        except Exception:
            pass                              # una linea corrupta no invalida el archivo
    return filas


def anadir(nuevas, mensaje, intentos=4):
    """Añade lineas al final. Reintenta releyendo si otro proceso escribio entre medias."""
    if not nuevas:
        log("  nada que anadir")
        return 0
    for i in range(intentos):
        filas, sha = leer_archivo()
        ya = {clave(f) for f in filas}
        pend = [n for n in nuevas if clave(n) not in ya]
        if not pend:
            log("  todas las lineas ya estaban (idempotente)")
            return 0
        texto = "".join(json.dumps(f, ensure_ascii=False, sort_keys=True) + "\n"
                        for f in filas + pend)
        if LOCAL and not DRY_RUN:
            os.makedirs(os.path.dirname(os.path.abspath(LOCAL)), exist_ok=True)
            with open(LOCAL, "w", encoding="utf-8") as fh:
                fh.write(texto)
            log(f"  escritas {len(pend)} lineas en {LOCAL} ({len(filas) + len(pend)} total)")
            return len(pend)
        if DRY_RUN:
            log(f"  [DRY_RUN] {len(pend)} lineas no escritas")
            for p in pend[:5]:
                log("    " + json.dumps(p, ensure_ascii=False)[:160])
            return len(pend)
        body = {"message": mensaje,
                "content": base64.b64encode(texto.encode()).decode()}
        if sha:
            body["sha"] = sha
        st, resp = http(f"https://api.github.com/repos/{GH_REPO}/contents/{RUTA}",
                        "PUT", _gh_headers(), body, timeout=60, retries=1)
        if st in (200, 201):
            log(f"  escritas {len(pend)} lineas ({len(filas) + len(pend)} en total)")
            return len(pend)
        if st in (409, 422):
            log(f"  conflicto de SHA (intento {i+1}) - releyendo")
            time.sleep(2 + 2 * i)
            continue
        raise RuntimeError(f"anadir {st}: {resp}")
    raise RuntimeError("anadir: demasiados conflictos de SHA")


def clave(f):
    """Identidad de una linea. Dos lineas con la misma clave son la misma observacion."""
    return (f.get("tipo"), (f.get("ticker") or "").upper(), f.get("fecha_evento"))


# -- utilidades --------------------------------------------------------------
def num(x):
    """Convierte a float. 999 es el centinela de EW para 'no hay whisper'."""
    if x in (None, "", 999, 999.0, "999"):
        return None
    try:
        v = float(x)
        return None if v == 999.0 else v
    except Exception:
        return None


def barras(ticker, dias=40):
    """Cierres diarios de la CINTA CONSOLIDADA (sip), no de iex.

    datasources.alpaca_daily_bars pide feed=iex, que es ~2-3% del volumen del mercado: sus
    cierres y sobre todo sus volumenes no son los reales. Para operar da igual, pero este
    fichero es material de investigacion y una reaccion mal medida contamina la muestra
    para siempre. Si sip no esta disponible en el plan, se cae a lo que haya.
    """
    h = {"APCA-API-KEY-ID": os.environ.get("APCA_KEY", ""),
         "APCA-API-SECRET-KEY": os.environ.get("APCA_SEC", "")}
    desde = (now_utc() - timedelta(days=int(dias * 1.8))).date().isoformat()
    for feed in ("sip", "iex"):
        st, d = http(f"https://data.alpaca.markets/v2/stocks/{ticker}/bars?timeframe=1Day"
                     f"&start={desde}&feed={feed}&adjustment=all&limit=1000", headers=h)
        bs = (d or {}).get("bars") or []
        if st == 200 and bs:
            return bs, feed
    try:
        return alpaca_daily_bars(ticker, days=dias), "iex_fallback"
    except Exception:
        return [], "ninguna"


def sesiones(alp, desde, n=20):
    cal = alp.req(f"/calendar?start={desde.isoformat()}&end={(desde + timedelta(days=n)).isoformat()}")
    return [date.fromisoformat(c["date"]) for c in cal] if isinstance(cal, list) else []


# -- fase A: capturar lo que aun no ha ocurrido ------------------------------
def capturar(alp, ses):
    """Snapshot de cada empresa que reporta en las proximas DIAS_VISTA sesiones."""
    filas, _ = leer_archivo()
    ya = {clave(f) for f in filas}
    out, vistos, gastado = [], set(), 0
    for d in ses[:DIAS_VISTA]:
        cal = ew_calendar(d)
        log(f"  calendario {d}: {len(cal)} empresas")
        for e in cal:
            t = (e.get("ticker") or "").upper().strip()
            if not t or t in vistos:
                continue
            vistos.add(t)
            if ("captura", t, d.isoformat()) in ya:
                continue
            if gastado >= MAX_TICKERS:
                continue
            gastado += 1
            # getstocksdata devuelve 404 para todos los tickers desde el 1-oct-2026.
            # Se sigue llamando por si vuelve, pero ya no es la unica fuente.
            s = ew_stock(t) or {}
            px = None
            try:
                bs, _f = barras(t, dias=5)
                px = float(bs[-1]["c"]) if bs else None
            except Exception:
                pass
            # El consenso tambien viene en el calendario (q1EstEPS): respaldo gratis y
            # ex-ante. El whisper, a 5-oct-2026, SOLO lo servia getstocksdata; el campo
            # existe en caldata pero llega nulo. Se intenta igualmente por si se rellena.
            w = num(s.get("whisper"))
            w_orig = "getstocksdata" if w is not None else None
            if w is None:
                w = num(e.get("whisper"))
                w_orig = "caldata" if w is not None else None
            cons = num(s.get("consensusEst"))
            c_orig = "getstocksdata" if cons is not None else None
            if cons is None:
                cons = num(e.get("q1EstEPS"))
                c_orig = "caldata" if cons is not None else None
            out.append({
                "tipo": "captura",
                "ticker": t,
                "fecha_evento": d.isoformat(),
                "capturado_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "empresa": e.get("company"),
                # 1 = antes de abrir, 3 = despues de cerrar
                "hora_publicacion": e.get("releaseTime") or s.get("releaseTime"),
                "n_analistas": e.get("total"),
                "whisper": w,
                "whisper_origen": w_orig,
                "consenso": cons,
                "consenso_origen": c_orig,
                # la variable que el sistema lleva un año dando por buena sin comprobarla.
                # abs() en el denominador: con consenso negativo (empresa en perdidas) dividir
                # sin valor absoluto invierte el signo y una buena noticia sale como negativa.
                "asimetria_pct": round((w - cons) / abs(cons) * 100, 3) if (w is not None and cons) else None,
                "ingresos_est": num(s.get("revenueEst")) or num(e.get("q1RevEst")),
                "mov_medio_eps": num(s.get("avgEPSMove")),
                "sector": s.get("sectName"),
                "trimestre": s.get("quarter"),
                "fin_trimestre": (s.get("quarterDate") or e.get("quarterDate") or "")[:10] or None,
                "fecha_confirmada": (e.get("confirmDate") or s.get("confirmDate") or "")[:10] or None,
                "eps_anterior_utc": (s.get("lastEPSTime") or "")[:16] or None,
                "precio_captura": px,
            })
    return out


# -- fase B: resolver lo que ya ocurrio --------------------------------------
def resolver(alp, ses, hoy):
    """Completa capturas pasadas con el resultado real y la reaccion del precio."""
    filas, _ = leer_archivo()
    caps = {(f["ticker"], f["fecha_evento"]): f for f in filas if f.get("tipo") == "captura"}
    hechas = {(f["ticker"], f["fecha_evento"]) for f in filas if f.get("tipo") == "resolucion"}
    res_hoy = {(r.get("ticker") or "").upper(): r for r in ew_results_today()}
    limite = hoy - timedelta(days=DIAS_RESOLVER)
    out = []
    for (t, fe), cap in caps.items():
        if (t, fe) in hechas:
            continue
        try:
            d_ev = date.fromisoformat(fe)
        except Exception:
            continue
        if d_ev >= hoy or d_ev < limite:
            continue                          # aun no ha pasado, o ya es demasiado viejo
        post = [s for s in ses if s > d_ev]
        if len(post) < 1:
            continue
        try:
            bars, feed = barras(t, dias=40)
        except Exception:
            bars, feed = [], "ninguna"
        px = {b["t"][:10]: float(b["c"]) for b in bars if b.get("t")}
        base = px.get(d_ev.isoformat()) or cap.get("precio_captura")
        if not base:
            continue

        def reac(n):
            if len(post) < n:
                return None
            p = px.get(post[n - 1].isoformat())
            return round((p / base - 1) * 100, 3) if p else None

        r1 = reac(1)
        if r1 is None:
            continue                # sin reaccion a 1 dia la fila no sirve; se reintenta manana
        r = res_hoy.get(t, {})
        eps_real = num(r.get("eps"))

        # Respaldo de la asimetria. todaysresults SI trae whisper y estimate, pero los
        # publica CON el resultado, no antes. No es equivalente a la captura ex-ante: hay
        # que poder analizar las dos poblaciones por separado, de ahi asimetria_origen.
        # coincide_exante deja medido, fila a fila, si EW reajusta el whisper a posteriori;
        # sin ese contraste el respaldo seria un acto de fe.
        w_post, c_post = num(r.get("whisper")), num(r.get("eps_consenso_ew") or r.get("estimate"))
        asim_post = (round((w_post - c_post) / abs(c_post) * 100, 3)
                     if (w_post is not None and c_post) else None)
        asim_ex = cap.get("asimetria_pct")
        if asim_ex is not None:
            asim, origen = asim_ex, "captura_exante"
        else:
            asim, origen = asim_post, ("todaysresults_post" if asim_post is not None else None)
        coincide = (None if (asim_ex is None or asim_post is None)
                    else abs(asim_ex - asim_post) < 0.01)

        out.append({
            "tipo": "resolucion",
            "ticker": t,
            "fecha_evento": fe,
            "resuelto_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "eps_real": eps_real,
            "eps_consenso_ew": num(r.get("estimate")),
            "eps_whisper_ew": num(r.get("whisper")),
            "sorpresa_eps_pct": num(r.get("earningsSurprise")),
            "sorpresa_ingresos_pct": num(r.get("revenueSurprise")),
            "batio_whisper": (None if (eps_real is None or (cap.get("whisper") or w_post) is None)
                              else eps_real > (cap.get("whisper") if cap.get("whisper") is not None else w_post)),
            "batio_consenso": (None if (eps_real is None or cap.get("consenso") is None)
                               else eps_real > cap["consenso"]),
            "asimetria_pct": asim,
            "asimetria_origen": origen,
            "asimetria_pct_post": asim_post,
            "coincide_exante": coincide,
            "fuente_precio": feed,
            "precio_base": base,
            "reaccion_1d_pct": r1,
            "reaccion_3d_pct": reac(3),
            "reaccion_5d_pct": reac(5),
        })
    return out


# -- informe: cuanta muestra hay, no que concluir ----------------------------
def informe():
    """No concluye nada: dice cuanta muestra hay y, sobre todo, si sigue ENTRANDO.

    La trampa que costo cuatro dias en octubre de 2026: el contador de pares completos
    subia mientras el archivo escribia fichas vacias. Subia porque se vaciaba el atasco
    de capturas viejas, no porque entrase nada. Un acumulado que crece no demuestra que
    el grifo este abierto; hay que mirar el caudal y el embudo, no el deposito.
    """
    filas, _ = leer_archivo()
    caps = [f for f in filas if f.get("tipo") == "captura"]
    res = {(f["ticker"], f["fecha_evento"]): f for f in filas if f.get("tipo") == "resolucion"}

    def asim_de(c):
        """Asimetria de un par: la ex-ante manda; si no hay, la de la resolucion."""
        r = res.get((c["ticker"], c["fecha_evento"])) or {}
        if c.get("asimetria_pct") is not None:
            return c["asimetria_pct"], "exante"
        if r.get("asimetria_pct") is not None:
            return r["asimetria_pct"], r.get("asimetria_origen") or "post"
        return None, None

    pares = []
    for c in caps:
        r = res.get((c["ticker"], c["fecha_evento"]))
        if not r or r.get("reaccion_1d_pct") is None:
            continue
        a, orig = asim_de(c)
        if a is not None:
            pares.append((a, r["reaccion_1d_pct"], orig))
    n = len(pares)
    n_ex = sum(1 for p in pares if p[2] == "exante")
    log(f"  archivo: {len(caps)} capturas, {len(res)} resoluciones, "
        f"{n} pares utilizables ({n_ex} ex-ante, {n - n_ex} de respaldo post-resultado)")

    # -- SALUD DEL CAUDAL: lo primero que hay que mirar ----------------------
    hoy_s = now_utc().strftime("%Y-%m-%d")
    hoy_caps = [c for c in caps if (c.get("capturado_utc") or "").startswith(hoy_s)]
    con_w = sum(1 for c in hoy_caps if c.get("whisper") is not None)
    pend_utiles = sum(1 for c in caps
                      if (c["ticker"], c["fecha_evento"]) not in res
                      and c.get("asimetria_pct") is not None)
    log(f"  caudal: {len(hoy_caps)} capturas hoy, {con_w} con whisper; "
        f"{pend_utiles} pendientes con asimetria ex-ante")
    if hoy_caps and con_w == 0 and pend_utiles == 0:
        log("  *** AVISO: 0 whisper hoy y 0 pendientes utiles. El archivo escribe fichas")
        log("      vacias y el contador esta CONGELADO. Solo avanzara por el respaldo")
        log("      post-resultado. Revisar si getstocksdata ha vuelto. ***")

    # -- fiabilidad del respaldo --------------------------------------------
    comp = [r.get("coincide_exante") for r in res.values() if r.get("coincide_exante") is not None]
    if comp:
        log(f"  respaldo post-resultado: coincide con la captura ex-ante en "
            f"{sum(1 for x in comp if x)}/{len(comp)} casos comparables")

    if n < 30:
        log(f"  muestra insuficiente para decir nada (n={n}). Sin conclusiones.")
        return
    import statistics as est
    a = [p[0] for p in pares]
    b = [p[1] for p in pares]
    ma, mb = est.mean(a), est.mean(b)
    sa, sb = est.pstdev(a), est.pstdev(b)
    rho = (sum((x - ma) * (y - mb) for x, y in zip(a, b)) / n / (sa * sb)) if sa and sb else 0.0
    ee = 1.96 / (n - 3) ** 0.5 if n > 3 else 9.9
    log(f"  correlacion asimetria->reaccion 1d: {rho:+.3f}  (error de Fisher +/-{ee:.2f}, n={n})")
    log(f"  {'HAY senal' if abs(rho) > ee else 'compatible con cero: todavia no hay senal'}")


def main():
    log("=== EW Archivo (observacional; no opera) ===")
    alp = paper_client()
    ts = (alp.req("/clock") or {}).get("timestamp", "")
    hoy = date.fromisoformat(ts[:10]) if ts else now_utc().date()
    ses = sesiones(alp, hoy - timedelta(days=DIAS_RESOLVER + 5), n=DIAS_RESOLVER + 25)
    futuras = [s for s in ses if s >= hoy]
    log(f"hoy {hoy}; proximas sesiones {[str(s) for s in futuras[:DIAS_VISTA]]}")

    nuevas = []
    try:
        nuevas += capturar(alp, futuras)
    except Exception as e:
        log(f"  fase captura fallo: {type(e).__name__}: {e}")
    try:
        nuevas += resolver(alp, ses, hoy)
    except Exception as e:
        log(f"  fase resolucion fallo: {type(e).__name__}: {e}")

    ncap = sum(1 for x in nuevas if x["tipo"] == "captura")
    nres = len(nuevas) - ncap
    log(f"a escribir: {ncap} capturas, {nres} resoluciones")
    anadir(nuevas, f"ew-archivo {hoy}: +{ncap} capturas, +{nres} resoluciones")
    try:
        informe()
    except Exception as e:
        log(f"  informe fallo: {type(e).__name__}: {e}")
    log("=== fin ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        sys.exit(1)
