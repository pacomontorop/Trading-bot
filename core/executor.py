# executor.py

from datetime import datetime
import time
import threading
import pandas as pd
import yfinance as yf
from broker.alpaca import api, get_current_price
from signals.filters import is_position_open, is_symbol_approved
from utils.logger import log_event, log_dir
from utils.daily_risk import (
    register_trade_pnl,
    is_risk_limit_exceeded,
    save_equity_snapshot,
    is_equity_drop_exceeded,
)
import os
import csv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

# Control de estado
open_positions = set()
pending_opportunities = set()
pending_trades = set()
executed_symbols_today = set()

# Locks for thread-safe access to the above sets
open_positions_lock = threading.Lock()
pending_opportunities_lock = threading.Lock()
pending_trades_lock = threading.Lock()
executed_symbols_today_lock = threading.Lock()
DAILY_INVESTMENT_LIMIT_PCT = 0.50
DAILY_MAX_LOSS_USD = 300.0  # Límite de pérdidas diarias
_last_investment_day = datetime.utcnow().date()
_total_invested_today = 0.0
_realized_pnl_today = 0.0
_last_equity_snapshot = None

quiver_signals_log = {}
# Store entry price, quantity and entry time for open positions to calculate PnL when they close
entry_data = {}

def reset_daily_investment():
    global _total_invested_today, _last_investment_day, executed_symbols_today, _realized_pnl_today
    today = datetime.utcnow().date()
    if today != _last_investment_day:
        _total_invested_today = 0.0
        _realized_pnl_today = 0.0
        _last_investment_day = today
        with executed_symbols_today_lock:
            executed_symbols_today.clear()

def add_to_invested(amount):
    global _total_invested_today
    _total_invested_today += amount

def invested_today_usd():
    return _total_invested_today


def get_adaptive_trail_price(symbol):
    """Calcula un trail_price dinámico basado en la volatilidad reciente."""
    try:
        hist = yf.download(symbol, period="5d", interval="1d", progress=False)
        if hist.empty or "Close" not in hist.columns:
            raise ValueError("No hay datos")
        current_price = hist["Close"].iloc[-1]
        std_pct = hist["Close"].pct_change().dropna().std()
        if pd.isna(std_pct) or std_pct <= 0:
            raise ValueError("Desviación inválida")
        std_pct = min(max(std_pct, 0.005), 0.05)
        return round(current_price * std_pct, 2)
    except Exception as e:
        print(f"⚠️ Error calculando trail adaptativo para {symbol}: {e}")
        fallback_price = get_current_price(symbol)
        return round(fallback_price * 0.015, 2)

def wait_for_order_fill(order_id, symbol, timeout=60):
    print(f"⌛ Empezando espera para orden {order_id} de {symbol}", flush=True)
    start = time.time()
    while time.time() - start < timeout:
        try:
            order = api.get_order(order_id)
            print(
                f"⌛ Orden {order_id} para {symbol} estado actual: {order.status}",
                flush=True,
            )
            if order.status == "filled":
                # Register entry price when the initial market order is filled
                if order.type == "market":
                    try:
                        entry_time = getattr(order, "filled_at", datetime.utcnow())
                        if isinstance(entry_time, str):
                            try:
                                entry_time = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                            except Exception:
                                entry_time = datetime.utcnow()
                        entry_data[symbol] = (
                            float(order.filled_avg_price),
                            float(order.qty),
                            entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                        )
                    except Exception:
                        pass

                # Calculate realized PnL when a trailing-stop order completes
                if order.type == "trailing_stop":
                    fill_price = float(getattr(order, "filled_avg_price", 0))
                    qty = float(order.qty)
                    avg_entry, _, date_in = entry_data.get(symbol, (None, None, None))
                    if avg_entry is not None:
                        if order.side == "sell":
                            pnl = (fill_price - avg_entry) * qty
                        else:
                            pnl = (avg_entry - fill_price) * qty
                        log_event(f"💰 PnL realized for {symbol}: {pnl:.2f} USD")
                        pnl_file = os.path.join(log_dir, "pnl.log")
                        os.makedirs(log_dir, exist_ok=True)
                        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                        with open(pnl_file, "a", encoding="utf-8") as pf:
                            pf.write(f"[{timestamp}] {symbol} {pnl:.2f}\n")

                        # Save closed trade details
                        exit_time = getattr(order, "filled_at", datetime.utcnow())
                        if isinstance(exit_time, str):
                            try:
                                exit_time = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
                            except Exception:
                                exit_time = datetime.utcnow()
                        exit_time_str = exit_time.strftime("%Y-%m-%d %H:%M:%S")

                        os.makedirs(DATA_DIR, exist_ok=True)
                        trades_path = os.path.join(DATA_DIR, "trades.csv")
                        file_exists = os.path.exists(trades_path)
                        signals = "|".join(quiver_signals_log.get(symbol, []))
                        with open(trades_path, "a", newline="", encoding="utf-8") as tf:
                            writer = csv.writer(tf)
                            if not file_exists:
                                writer.writerow([
                                    "symbol",
                                    "entry_price",
                                    "exit_price",
                                    "qty",
                                    "pnl_usd",
                                    "date_in",
                                    "date_out",
                                    "signals",
                                ])
                            writer.writerow([
                                symbol,
                                avg_entry,
                                fill_price,
                                qty,
                                round(pnl, 2),
                                date_in,
                                exit_time_str,
                                signals,
                            ])

                        entry_data.pop(symbol, None)
                        global _realized_pnl_today
                        _realized_pnl_today += pnl
                        register_trade_pnl(symbol, pnl)
                return True
            elif order.status in ["canceled", "rejected"]:
                reason = getattr(order, "reject_reason", "Sin motivo")
                print(
                    f"❌ Orden {order_id} para {symbol} cancelada o rechazada: {order.status} - {reason}",
                    flush=True,
                )
                log_event(
                    f"❌ Falló la orden para {symbol}: {order.status} - {reason}"
                )
                return False
        except Exception as e:
            log_event(
                f"❌ Error verificando estado de orden {order_id} para {symbol}: {e}"
            )
        time.sleep(1)
    print(
        f"⚠️ Timeout esperando ejecución de orden {order_id} para {symbol}",
        flush=True,
    )
    log_event(f"⚠️ Timeout esperando fill para {symbol}")
    return False

def place_order_with_trailing_stop(symbol, amount_usd, trail_percent=1.5):
    global _last_equity_snapshot
    reset_daily_investment()
    today = datetime.utcnow().date()
    if _last_equity_snapshot != today:
        save_equity_snapshot()
        _last_equity_snapshot = today
    if is_equity_drop_exceeded(5.0):
        log_event(
            "🛑 STOP automático: equity cayó más de 5% respecto a ayer. No se operará hoy."
        )
        return False
    if is_risk_limit_exceeded():
        log_event("⚠️ Límite de pérdidas diarias alcanzado. No se operará más hoy.")
        return False
    if _realized_pnl_today < -DAILY_MAX_LOSS_USD:
        log_event(
            f"⛔ Límite diario de pérdidas alcanzado: {_realized_pnl_today:.2f} USD"
        )
        return False
    print(f"\n🚀 Iniciando proceso de compra para {symbol} por ${amount_usd}...")
    try:
        if not is_symbol_approved(symbol):
            print(f"❌ {symbol} no aprobado para compra según criterios de análisis.")
            return False

        print(f"✅ {symbol} pasó todos los filtros iniciales. Obteniendo señales finales...")

        from signals.quiver_utils import (
            get_all_quiver_signals,
            score_quiver_signals,
            QUIVER_APPROVAL_THRESHOLD,
            get_adaptive_take_profit,
        )
        quiver_signals = get_all_quiver_signals(symbol)
        quiver_score = score_quiver_signals(quiver_signals)
        quiver_signals_log[symbol] = [k for k, v in quiver_signals.items() if v]

        account = api.get_account()
        equity = float(account.equity)
        buying_power = float(getattr(account, "buying_power", account.cash))

        try:
            asset = api.get_asset(symbol)
            if not getattr(asset, "tradable", True):
                print(f"⛔ {symbol} no es tradable en Alpaca.", flush=True)
                return False
        except Exception as e:
            print(f"❌ Error obteniendo información de {symbol}: {e}", flush=True)
            return False

        # Nueva excepción: si Quiver score es muy alto (> 10), ignorar límite
        if invested_today_usd() + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT and quiver_score < 10:
            print("⛔ Límite de inversión alcanzado para hoy y Quiver score < 10.")
            return False
        elif invested_today_usd() + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT:
            print(f"⚠️ {symbol} excede límite pero Quiver score = {quiver_score} ➜ Se permite excepcionalmente.")

        with open_positions_lock, executed_symbols_today_lock:
            if symbol in open_positions or symbol in executed_symbols_today:
                print(f"⚠️ {symbol} ya ejecutado o con posición abierta.")
                return False

        if is_position_open(symbol):
            print(f"⚠️ Ya hay una posición abierta en {symbol}. No se realiza nueva compra.")
            return False

        current_price = get_current_price(symbol)
        if not current_price:
            print(f"❌ Precio no disponible para {symbol}")
            return False

        if amount_usd > buying_power:
            print(
                f"⛔ Fondos insuficientes para comprar {symbol}: requieren {amount_usd}, disponible {buying_power}",
                flush=True,
            )
            return False

        qty = int(amount_usd // current_price)
        if qty == 0:
            print(f"⚠️ Fondos insuficientes para comprar {symbol}", flush=True)
            return False

        print(
            f"🛒 Orden de compra -> {symbol} {qty}×${current_price:.2f}",
            flush=True,
        )
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='buy',
            type='market',
            time_in_force='gtc'
        )
        print(
            f"📨 Orden enviada: ID {order.id}, estado inicial {order.status}",
            flush=True,
        )
        log_event(f"✅ Orden enviada para {symbol}")
        print(
            "⌛ Esperando a que se rellene la orden...",
            flush=True,
        )
        if not wait_for_order_fill(order.id, symbol):
            return False
        entry_price, _, _ = entry_data.get(symbol, (current_price, None, None))
        take_profit = get_adaptive_take_profit(symbol, entry_price, quiver_score)
        if take_profit:
            print(
                f"🎯 Colocando take profit para {symbol} en ${take_profit:.2f}",
                flush=True,
            )
            api.submit_order(
                symbol=symbol,
                qty=qty,
                side='sell',
                type='limit',
                time_in_force='gtc',
                limit_price=take_profit,
            )

        trail_price = get_adaptive_trail_price(symbol)
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell',
            type='trailing_stop',
            time_in_force='gtc',
            trail_price=trail_price
        )

        with open_positions_lock:
            open_positions.add(symbol)
        add_to_invested(qty * current_price)
        with executed_symbols_today_lock:
            executed_symbols_today.add(symbol)
        with pending_trades_lock:
            pending_trades.add(f"{symbol}: {qty} unidades — ${qty * current_price:.2f}")

        log_event(
            f"✅ Compra y trailing stop colocados para {symbol}: {qty} unidades por {qty * current_price:.2f} USD (Quiver score: {quiver_score})"
        )
        return True

    except Exception as e:
        log_event(f"❌ Falló la orden para {symbol}: {e}")
        return False


def place_short_order_with_trailing_buy(symbol, amount_usd, trail_percent=1.5):
    reset_daily_investment()
    if is_risk_limit_exceeded():
        log_event("⚠️ Límite de pérdidas diarias alcanzado. No se operará más hoy.")
        return
    if _realized_pnl_today < -DAILY_MAX_LOSS_USD:
        log_event(
            f"⛔ Límite diario de pérdidas alcanzado: {_realized_pnl_today:.2f} USD"
        )
        return
    print(f"\n🚀 Iniciando proceso de short para {symbol} por ${amount_usd}...")
    try:
        if not is_symbol_approved(symbol):
            print(f"❌ {symbol} no aprobado para short según criterios de análisis.")
            return

        print(f"✅ {symbol} pasó filtros iniciales para short. Obteniendo señales finales...")

        from signals.quiver_utils import get_all_quiver_signals
        quiver_signals_log[symbol] = [
            k for k, v in get_all_quiver_signals(symbol).items() if v
        ]

        account = api.get_account()
        equity = float(account.equity)

        if invested_today_usd() + amount_usd > equity * DAILY_INVESTMENT_LIMIT_PCT:
            print("⛔ Límite de inversión alcanzado para hoy.")
            return

        with open_positions_lock, executed_symbols_today_lock:
            if symbol in open_positions or symbol in executed_symbols_today:
                print(f"⚠️ {symbol} ya ejecutado o con posición abierta.")
                return

        if is_position_open(symbol):
            print(f"⚠️ Ya hay una posición abierta en {symbol}. No se realiza nuevo short.")
            return

        current_price = get_current_price(symbol)
        if not current_price:
            print(f"❌ Precio no disponible para {symbol}")
            return

        qty = int(amount_usd // current_price)
        if qty == 0:
            print(f"⚠️ Fondos insuficientes para short en {symbol}")
            return

        print(f"📉 Enviando orden SHORT para {symbol} por ${amount_usd} → {qty} unidades a ${current_price:.2f} cada una.")
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell',
            type='market',
            time_in_force='gtc'
        )

        if not wait_for_order_fill(order.id, symbol):
            return

        trail_price = round(current_price * (trail_percent / 100), 2)
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side='buy',
            type='trailing_stop',
            time_in_force='gtc',
            trail_price=trail_price
        )

        with open_positions_lock:
            open_positions.add(symbol)
        add_to_invested(qty * current_price)
        with executed_symbols_today_lock:
            executed_symbols_today.add(symbol)
        with pending_trades_lock:
            pending_trades.add(f"{symbol} SHORT: {qty} unidades — ${qty * current_price:.2f}")

        log_event(f"✅ Short y trailing buy colocados para {symbol}: {qty} unidades por {qty * current_price:.2f} USD")

    except Exception as e:
        log_event(f"❌ Falló la orden para {symbol}: {e}")
