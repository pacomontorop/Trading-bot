# CLAUDE.md — Trading-bot System Guide (Guía Completa para Claude Cowork)

## ¿Qué es este bot?

**Trading bot long-only** (solo compras, sin shorts) para acciones de EE.UU.
- Usa **Alpaca** como broker (paper y live en paralelo)
- Señales de **QuiverQuant** (insiders, contratos gov, congresistas) + **Yahoo Finance** (técnico)
- Ejecuta **bracket orders** con gestión de riesgo basada en ATR
- Corre en **Render** como servicio web (uvicorn, 1 worker, autodeploy desde `main`)
- Desplegado en: `uvicorn start:app --host 0.0.0.0 --workers 1`

---

## Arquitectura Completa

```
main.py / start.py              ← entry points (main.py = directo, start.py = FastAPI + healthcheck)
core/scheduler.py               ← loop principal cada 60 segundos
signals/reader.py               ← orquesta scoring: universo → features → score → aprobación
signals/features.py             ← agrega features numéricas (sin decisiones)
signals/scoring.py              ← fetch Yahoo snapshot (precio, volumen, ATR, historia)
signals/quiver_utils.py         ← extrae features de QuiverQuant
signals/quiver_ingest.py        ← llamadas API a QuiverQuant
signals/quiver_throttler.py     ← rate limiting Quiver
signals/gates.py                ← gates legacy (mínimo)
signals/filters.py              ← chequeo posición abierta

core/risk_manager.py            ← cuenta paper: sizing, límites diarios, estado
core/executor.py                ← cuenta paper: colocar órdenes bracket
core/position_protector.py      ← cuenta paper: trailing-stop y break-even
broker/alpaca.py                ← lazy REST wrapper (paper)

core/live_risk_manager.py       ← cuenta live: sizing conservador, estado
core/live_executor.py           ← cuenta live: colocar órdenes + protección
broker/alpaca_live.py           ← lazy REST wrapper (live → api.alpaca.markets)

core/market_gate.py             ← ¿mercado abierto? ¿VIX elevado?
core/safeguards.py              ← TTL-based safety lock (bloquea órdenes si expira)
core/order_protection.py        ← matemáticas bracket: precios stop/TP, redondeo ticks
core/broker.py                  ← helpers tick size

config/policy.yaml              ← TODOS los parámetros configurables (nunca hardcodear)
config.py                       ← carga env vars + policy.yaml
.env                            ← secrets (NUNCA commitear)

data/risk_state.json            ← estado diario paper (gasto, posiciones, cooldowns)
data/risk_state_live.json       ← estado diario live
data/paper_stop_hwm.json        ← high-water mark de stops paper (stops solo suben)
data/live_stop_hwm.json         ← high-water mark de stops live
data/symbols.csv                ← universo ~4900 acciones (auto-generado)
logs/events.log                 ← stream de eventos JSON estructurado

utils/logger.py                 ← logging estructurado
utils/symbols.py                ← normalización de tickers
utils/cache.py                  ← caché en memoria con TTL
utils/telegram_alert.py         ← alertas Telegram
utils/generate_symbols_csv.py   ← genera symbols.csv desde Alpaca

tests/conftest.py               ← credenciales dummy para tests
tests/test_bracket_payload.py   ← cálculos bracket
tests/test_gate_selfcheck.py    ← gates de mercado/posición
tests/test_blown_stop.py        ← detección de stops saltados
tests/test_signal_verification.py ← scoring de features, gates
```

---

## Flujo Completo: De Señal a Orden

### 1. Tick del Scheduler (cada 60s)

```
core/scheduler.py::equity_scheduler_loop()
│
├─ ¿Mercado abierto? → market_gate.is_us_equity_market_open()  (caché 15s)
├─ ¿VIX elevado?    → market_gate.get_vix_level()              (caché 10m)
│   Si VIX > vix_pause_threshold → skip ciclo, NO nuevas órdenes
│   (posiciones existentes siguen protegidas)
│
├─ PROTECCIÓN DE POSICIONES (independiente del scan)
│   ├─ Paper: position_protector.tick_protect_positions()
│   └─ Live:  live_executor.tick_protect_live_positions()
│
└─ SCAN DE SEÑALES
    ├─ signals/reader.get_top_signals(max_symbols=30)
    ├─ Para cada señal aprobada → executor.place_long_order(plan)
    └─ Si ENABLE_LIVE_TRADING → live_executor.place_live_order(plan)
```

### 2. Pipeline de Señales (`signals/reader.py`)

```
Universo ~4900 símbolos (rotación diaria con semilla fija)
│
├─ BATCH de 30 símbolos (cooldown 4h por símbolo, rotación completa antes de repetir)
│
├─ Para cada símbolo:
│   │
│   ├─ GATE 1: Yahoo Liquidez & Tendencia
│   │   ├─ Cap mín: $1B (o $300M si señal Quiver fuerte)
│   │   ├─ Volumen 7d mín: 250k acciones (o 50k)
│   │   ├─ ATR% máx: 6% (o 12%)
│   │   ├─ Tendencia positiva vs hace 90 días (LONG-ONLY)
│   │   └─ Precio: $5 - $1000
│   │
│   ├─ GATE 2: Quiver Mínimo
│   │   ├─ Score Quiver ≥ 3.0
│   │   └─ ≥ 2 tipos distintos de señal activos (correlación)
│   │
│   ├─ GATE 3: RSI Técnico (desactivado por defecto)
│   │   └─ 40 ≤ RSI ≤ 75
│   │
│   ├─ FAST-LANE (bypass umbrales estrictos Yahoo si señal Quiver muy fuerte)
│   │   ├─ 1er scan: registra timestamp señal fuerte
│   │   └─ 2do scan < 300s: CONFIRMA y activa fast-lane
│   │       (aún requiere tendencia positiva + score Quiver mínimo)
│   │
│   └─ SCORING FINAL
│       ├─ Senado compra:      2.8 pts/conteo  (máx 14 pts)
│       ├─ Congreso compra:    1.8 pts/conteo  (máx 9 pts)
│       ├─ Insiders neto:      2.0 pts/conteo  (máx 10 pts)
│       ├─ Insiders compras:   1.0 pts/conteo  (máx 5 pts)
│       ├─ Insiders ventas:   -1.5 pts/conteo  (máx -7.5 pts)
│       ├─ Gov contratos USD:  0.0000025×USD   (máx 12.5 pts)
│       ├─ Gov contratos #:    0.8 pts/conteo  (máx 4 pts)
│       ├─ Momentum patentes:  1.5 pts/score   (máx 7.5 pts)
│       ├─ SEC 13F conteo:     0.15 pts/conteo (lag trimestral)
│       ├─ WSB menciones:      0.005 pts/mención (máx 2.5 pts)
│       ├─ Encima SMA50:       2.5 pts (binario)
│       ├─ RSI signal:         1.2 pts (mejor zona 30-50)
│       └─ Momentum 20d:       0.08 pts/% (máx 2.4 pts)
│
│   UMBRAL APROBACIÓN: ≥ 9.8 puntos
│
└─ APROBADO → verificar riesgo → colocar orden
```

### 3. Gestión de Riesgo Paper (`core/risk_manager.py`)

Antes de cada orden, verifica:
1. Gasto diario ≤ `daily_max_spend_usd` (o % de buying power)
2. Nuevas posiciones < `daily_max_new_positions`
3. Posiciones totales < `max_total_open_positions`
4. Exposición total ≤ `max_exposure_pct_equity × equity`
5. Exposición por símbolo ≤ `max_symbol_exposure_pct_equity × equity`
6. Cooldown de símbolo: ≥ `symbol_cooldown_days` días desde último trade
7. No hay orden abierta para ese símbolo

**Sizing de posición:**
```
stop_distance = max(atr_k × ATR, min_stop_pct × precio)
risk_budget   = equity × max_symbol_risk_pct
qty           = risk_budget / stop_distance
notional      = min(qty × precio, equity × max_symbol_exposure_pct_equity)
```

### 4. Ejecución Bracket (`core/executor.py`)

```
entry = precio mercado
stop  = entry - max(atr_k × ATR, min_stop_pct × entry)   → "1.5×ATR abajo"
TP    = entry + take_profit_atr_mult × ATR                → "2.5×ATR arriba"
R:R   = (TP - entry) / (entry - stop) ≥ min_rr_ratio (1.2)

api.submit_order(type="market", order_class="bracket",
    take_profit={"limit_price": TP},
    stop_loss={"stop_price": stop, "limit_price": stop_limit})
```

Requisito: `safeguards.enabled=true` Y TTL no expirado → sino BLOQUEA.

### 5. Protección de Posiciones (cada 60s)

**Break-even** (cuando precio ≥ entry + 0.2R):
```
new_stop = entry × 1.002   (0.2% por encima del entry)
→ nunca más pierde dinero en esa posición
```

**Trailing stop** (cuando precio ≥ entry + 1.5R):
```
Reemplaza stop fijo → trailing_stop al 2%
→ sigue al precio hacia arriba automáticamente
```

**High-water mark**: Los stops NUNCA bajan (archivo `paper_stop_hwm.json`).

---

## Cuenta Live (Trading Real)

Activar con `ENABLE_LIVE_TRADING=true` + claves reales en `.env`.

- **Mismas señales aprobadas** que paper, sizing independiente y conservador
- Budget: `min(cash × max_cash_pct=10%, max_position_size_usd=$350)`
- Estado separado: `data/risk_state_live.json`
- Protección separada: `data/live_stop_hwm.json`
- Lock de proceso: `data/live_protect.lock` (evita instancias duplicadas)
- URL: `https://api.alpaca.markets`

---

## Safeguards TTL (Sistema de Seguridad)

`config/policy.yaml`:
```yaml
safeguards:
  enabled: true
  started_at_utc: "2026-03-19T00:00:00Z"
  ttl_days: 90   # expira ~2026-06-17
```

- Si `now > started_at_utc + ttl_days` → **TODAS las órdenes bloqueadas**
- Las posiciones existentes siguen protegidas (trailing, break-even)
- **Fix**: actualizar `started_at_utc` a fecha de hoy

---

## Variables de Entorno

| Variable | Requerida | Default | Descripción |
|---|---|---|---|
| `APCA_API_KEY_ID` | **SÍ** | — | Alpaca paper key |
| `APCA_API_SECRET_KEY` | **SÍ** | — | Alpaca paper secret |
| `APCA_API_BASE_URL` | no | paper URL | URL Alpaca |
| `APCA_API_KEY_ID_REAL` | si live | — | Alpaca live key |
| `APCA_API_SECRET_KEY_REAL` | si live | — | Alpaca live secret |
| `ENABLE_LIVE_TRADING` | no | false | Activar cuenta real |
| `ENABLE_QUIVER` | no | true | Usar QuiverQuant |
| `ENABLE_YAHOO` | no | true | Usar Yahoo Finance |
| `ENABLE_FMP` | no | false | Usar FMP (opcional) |
| `QUIVER_API_KEY` | si Quiver | — | API key QuiverQuant |
| `FMP_API_KEY` | si FMP | — | API key FMP |
| `DRY_RUN` | no | false | Simular sin órdenes reales |
| `DAILY_RISK_LIMIT` | no | -200 | Pérdida máxima diaria USD |
| `REDIS_URL` | no | — | Caché distribuida Redis |
| `MONITOR_INTERVAL` | no | 60 | Intervalo loop (seg) |
| `TELEGRAM_BOT_TOKEN` | no | — | Alertas Telegram |
| `TELEGRAM_CHAT_ID` | no | — | Chat Telegram |
| `WEB_CONCURRENCY` | no | 1 | **DEBE ser 1** (bot stateful) |

---

## Parámetros Clave `config/policy.yaml`

### Señales
```yaml
signals:
  max_symbols_per_scan: 30          # ~2 min por batch
  symbol_rescan_cooldown_hours: 4   # 2-3 sweeps completos por día
  approval_threshold: 9.8           # puntos mínimos para aprobar
  min_quiver_score: 3.0             # score Quiver mínimo
```

### Mercado
```yaml
market:
  global_kill_switch: false         # true = PARA TODO inmediatamente
  vix_pause_threshold: 22           # 0 = desactivado; >22 = pausa nuevas entradas
```

### Riesgo Paper
```yaml
risk:
  atr_k: 1.5                        # stop = entry - 1.5×ATR
  min_stop_pct: 0.03                # stop mínimo 3% del precio
  take_profit_atr_mult: 2.5         # TP = entry + 2.5×ATR
  max_symbol_exposure_pct_equity: 0.12  # máx 12% por posición
  max_symbol_risk_pct: 0.01         # arriesgar máx 1% del equity
  symbol_cooldown_days: 5
  min_rr_ratio: 1.2                 # R:R mínimo 1.2
```

### Live
```yaml
live_account:
  max_cash_pct: 0.10                # 10% del cash por trade
  max_position_size_usd: 350        # cap duro por posición live
  daily_max_cash_pct: 0.30          # 30% del cash total por día
  symbol_cooldown_days: 5
```

---

## Persistencia de Estado

| Archivo | Qué guarda |
|---|---|
| `data/risk_state.json` | Gasto hoy, posiciones hoy, símbolos operados, cooldowns (paper) |
| `data/risk_state_live.json` | Mismo para cuenta live |
| `data/paper_stop_hwm.json` | Nivel más alto de stop por símbolo (paper) — stops nunca bajan |
| `data/live_stop_hwm.json` | Mismo para live |
| `data/paper_protect.lock` | Lock de proceso para evitar doble protección |
| `data/live_protect.lock` | Lock proceso live |
| `data/symbols.csv` | Universo ~4900 acciones (regenerado diariamente) |
| `logs/events.log` | Stream JSON de todos los eventos: SCAN, ORDER, PROTECT, RISK, ERROR |

---

## Razones por las que se Rechazan Órdenes

| Rechazo | Fase | Causa |
|---|---|---|
| `yahoo_prefilter` | Señal | Cap <$1B, volumen <250k, precio fuera $5-$1000 |
| `yahoo_stale` | Señal | Datos >2 días de antigüedad |
| `trend_negative` | Señal | Precio hoy < precio hace 90 días |
| `atr_pct_high` | Señal | Volatilidad >6% |
| `quiver_min_signal` | Señal | Score Quiver <3.0 |
| `quiver_min_types` | Señal | <2 tipos de señal activos |
| `fast_lane_pending` | Señal | Señal fuerte en 1er scan, esperando confirmación |
| `position_open` | Riesgo | Ya hay posición abierta en ese símbolo |
| `order_pending` | Riesgo | Hay orden de compra pendiente |
| `symbol_cooldown` | Riesgo | <5 días desde último trade |
| `daily_spend_exceeded` | Riesgo | Gasto diario superado |
| `max_exposure_exceeded` | Riesgo | Exposición total del portfolio superada |
| `safeguards_inactive` | Pre-orden | TTL expirado o safeguards desactivados |
| `zero_qty` | Ejecución | Tamaño de posición redondeado a 0 |
| `invalid_bracket` | Ejecución | Stop ≥ entry o TP ≤ entry |

---

## Problemas Comunes y Fix

### Órdenes bloqueadas: `safeguards_inactive`
```yaml
# config/policy.yaml
safeguards:
  started_at_utc: "2026-03-24T00:00:00Z"  # actualizar a hoy
  ttl_days: 90
```

### Import error: `ValueError: Key ID must be given`
Las credenciales Alpaca no están en `.env`. El broker usa lazy init (se conecta en primer uso).

### `ModuleNotFoundError: No module named 'alpaca_trade_api'`
```bash
pip install --only-binary=:all: msgpack
pip install --no-deps alpaca-trade-api
```

### Múltiples instancias en Render → spam de órdenes rechazadas
Start Command debe ser:
```
uvicorn start:app --host 0.0.0.0 --workers 1
```
**NO usar `--port $PORT`** (Render tiene bug con esa variable). `WEB_CONCURRENCY=1` en env vars.

### `data/symbols.csv` missing
Se auto-genera en primer run. Asegurarse que el directorio `data/` existe.

---

## Tests

```bash
python -m pytest tests/ -v          # todos
python -m pytest tests/ -v -k gate  # solo gates
```

- `tests/conftest.py` — credenciales dummy (no necesita keys reales)
- `tests/test_bracket_payload.py` — cálculos bracket, redondeo, R:R
- `tests/test_gate_selfcheck.py` — gates de mercado y posición
- `tests/test_blown_stop.py` — detección de gaps/stops saltados
- `tests/test_signal_verification.py` — scoring, gates, features

---

## Deploy en Render

- **Start Command**: `uvicorn start:app --host 0.0.0.0 --workers 1`
- **Healthcheck**: `GET /` → `{"status": "ok"}`
- **WEB_CONCURRENCY=1** (obligatorio — bot stateful con archivos en disco)
- Autodeploy activado desde branch `main`
- Paper URL: `https://paper-api.alpaca.markets`
- Live URL: `https://api.alpaca.markets`

---

## Branch & Git

- Branch de desarrollo: `claude/complete-system-improvements-QWF37`
- Branch de producción: `main` (Render hace autodeploy desde aquí)
- Flujo: commit → push branch → merge main → Render autodeploy automático

---

## Historial de Mejoras (cronológico)

1. **Lazy broker init** — `broker/alpaca.py` conecta en primer uso (tests sin keys)
2. **Test fixtures** — `tests/conftest.py` con credenciales dummy
3. **Safeguards TTL reset** — `started_at_utc` actualizado
4. **Requirements pinned** — versiones exactas para Python 3.11
5. **`.env.example` expandido** — documenta todas las variables
6. **VIX fear gate** — pausa nuevas entradas cuando VIX > umbral
7. **Live trading paralelo** — cuenta real independiente en mismo loop
8. **Fix stop HWM** — stops nunca bajan cuando expira bracket day-TIF
9. **Fix STAA spam** — suppress en cancel_failed + detección async rejection
10. **Fix price staleness** — snapshot obsoleto causaba market-sell HBAN
11. **Fix live async rejection** — no market-sell si falla cancel
12. **Fix cancel_all_sells_and_wait** — robusto para legs TP de bracket

---

## Code Style

- Python 3.11+, `from __future__ import annotations` en cada módulo
- Todas las funciones públicas tienen type hints
- Llamadas de red en `try/except` con fallback logging
- Sin estado global excepto broker singleton y config de policy
- **Cambios de configuración**: editar `config/policy.yaml` — NUNCA hardcodear valores

---

## ⚠️ LECCIONES CRÍTICAS DEL SISTEMA (NO IGNORAR)

### MEDP 2026-04-23 — Gap nocturno post-earnings: stop_limit NO protege
- **Qué pasó:** MEDP comprada ~$515. Reportó earnings AH con EPS beat pero guidance solo "afirmado". 
  Stock cayó $515→$420 en after-hours. Stop_limit a $488 NO se ejecutó (precio saltó al otro lado del límite).
- **Pérdida:** -18.6%, -$957 (paper)
- **Causa raíz:** 
  1. Bot no verifica si el ticker tiene earnings AH ese día antes de mantener posición overnight.
  2. Bracket stop_limit no protege contra gaps: price gaps past both stop AND limit.
  3. Bot corre solo durante horas de mercado → AH no hay protección activa.
- **Fix necesario en código:**
  - `core/scheduler.py`: antes de `tick_protect_positions`, verificar si alguna posición abierta tiene 
    earnings AH hoy (via `yf.Ticker(sym).calendar`). Si sí → close at market antes de 15:45 ET.
  - `signals/reader.py` o `core/executor.py`: no abrir posición en ticker con earnings en las próximas 24h.
- **Config ya actualizado:** `policy.yaml` → `earnings.close_before_ah_earnings: true` (pendiente impl. en código)

### Regla de oro: posición + earnings AH = CERRAR antes de las 15:45 ET

