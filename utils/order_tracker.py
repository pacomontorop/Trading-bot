import os
from datetime import datetime, date
from typing import Tuple

import pandas as pd
import yfinance as yf

from broker.alpaca import api
from utils.logger import log_event

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDERS_FILE = os.path.join(PROJECT_ROOT, "logs", "orders_history.csv")
HEADERS = [
    "symbol",
    "fecha_entrada",
    "tipo",
    "precio_entrada",
    "shares",
    "fecha_salida",
    "precio_salida",
    "resultado",
]


def _ensure_file() -> None:
    os.makedirs(os.path.dirname(ORDERS_FILE), exist_ok=True)
    if not os.path.exists(ORDERS_FILE):
        pd.DataFrame(columns=HEADERS).to_csv(ORDERS_FILE, index=False)


def _load_df() -> pd.DataFrame:
    _ensure_file()
    return pd.read_csv(ORDERS_FILE)


def _save_df(df: pd.DataFrame) -> None:
    df.to_csv(ORDERS_FILE, index=False)


def record_today_orders() -> pd.DataFrame:
    """Record today's filled orders from Alpaca into the CSV."""
    today = date.today()
    df = _load_df()
    try:
        orders = api.list_orders(status="filled", limit=500)
    except Exception as e:
        log_event(f"❌ Error fetching orders: {e}")
        return df
    for order in orders:
        if not order.filled_at:
            continue
        filled_date = order.filled_at.date()
        if filled_date != today:
            continue
        entry_date_str = filled_date.isoformat()
        exists = (
            (df["symbol"] == order.symbol)
            & (df["fecha_entrada"] == entry_date_str)
        ).any()
        if exists:
            continue
        side = "long" if order.side == "buy" else "short"
        row = {
            "symbol": order.symbol,
            "fecha_entrada": entry_date_str,
            "tipo": side,
            "precio_entrada": float(order.filled_avg_price),
            "shares": int(order.qty),
            "fecha_salida": "",
            "precio_salida": "",
            "resultado": "",
        }
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    _save_df(df)
    return df


def _fetch_close_price(symbol: str) -> float | None:
    try:
        data = yf.download(symbol, period="1d", progress=False)
        if not data.empty:
            return float(data["Close"].iloc[-1])
    except Exception:
        pass
    return None


def record_trade_result(
    symbol: str,
    entry_price: float,
    exit_price: float,
    shares: float,
    side: str,
    entry_date: str,
    exit_date: str,
) -> None:
    """Update ``orders_history.csv`` with the result of a closed trade.

    Args:
        symbol: traded ticker symbol
        entry_price: price at which the position was opened
        exit_price: price at which the position was closed
        shares: number of shares traded
        side: "long" or "short"
        entry_date: date of entry in ``YYYY-MM-DD`` format
        exit_date: date of exit in ``YYYY-MM-DD`` format
    """

    df = _load_df()
    mask = (df["symbol"] == symbol) & (df["fecha_entrada"] == entry_date)
    if mask.any():
        idx = df[mask].index[-1]
    else:
        row = {
            "symbol": symbol,
            "fecha_entrada": entry_date,
            "tipo": side,
            "precio_entrada": float(entry_price),
            "shares": float(shares),
            "fecha_salida": "",
            "precio_salida": "",
            "resultado": "",
        }
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        idx = df.index[-1]

    df.at[idx, "precio_entrada"] = float(entry_price)
    df.at[idx, "shares"] = float(shares)
    df.at[idx, "precio_salida"] = round(float(exit_price), 2)
    df.at[idx, "fecha_salida"] = exit_date
    df.at[idx, "tipo"] = side

    if side == "long":
        result = "ganadora" if exit_price > entry_price else "perdedora"
    else:
        result = "ganadora" if exit_price < entry_price else "perdedora"
    df.at[idx, "resultado"] = result
    _save_df(df)

def verify_old_orders(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Check orders older than 3 days without result and mark them."""
    today = date.today()
    if df is None:
        df = _load_df()
    updated = False
    for idx, row in df.iterrows():
        if pd.notna(row.get("resultado")) and row.get("resultado"):
            continue
        try:
            entry_date = datetime.strptime(row["fecha_entrada"], "%Y-%m-%d").date()
        except Exception:
            continue
        if (today - entry_date).days < 3:
            continue
        price = _fetch_close_price(row["symbol"])
        if price is None:
            continue
        entry_price = float(row["precio_entrada"])
        shares = float(row["shares"])
        if row["tipo"] == "long":
            pnl = (price - entry_price) * shares
            result = "ganadora" if price > entry_price else "perdedora"
        else:
            pnl = (entry_price - price) * shares
            result = "ganadora" if price < entry_price else "perdedora"
        df.at[idx, "fecha_salida"] = today.isoformat()
        df.at[idx, "precio_salida"] = round(price, 2)
        df.at[idx, "resultado"] = result
        log_event(
            f"🔍 Orden {row['symbol']} cerrada con PnL {pnl:.2f} ({result})"
        )
        updated = True
    if updated:
        _save_df(df)
    return df


def compute_cumulative_stats() -> Tuple[int, int, int, float]:
    """Update history and return cumulative stats.

    Returns:
        total_orders: total recorded orders
        winners: count of winning trades
        losers: count of losing trades
        pnl_total: total realized PnL
    """
    df = record_today_orders()
    df = verify_old_orders(df)
    total_orders = len(df)
    closed = df[df["resultado"].isin(["ganadora", "perdedora"])]
    winners = (closed["resultado"] == "ganadora").sum()
    losers = (closed["resultado"] == "perdedora").sum()
    if not closed.empty:
        prices_exit = pd.to_numeric(closed["precio_salida"], errors="coerce")
        prices_entry = pd.to_numeric(closed["precio_entrada"], errors="coerce")
        shares = pd.to_numeric(closed["shares"], errors="coerce")
        pnl_series = (prices_exit - prices_entry) * shares
        shorts = closed["tipo"] == "short"
        pnl_series[shorts] = (prices_entry[shorts] - prices_exit[shorts]) * shares[shorts]
        pnl_total = float(pnl_series.sum())
    else:
        pnl_total = 0.0
    return total_orders, int(winners), int(losers), pnl_total

