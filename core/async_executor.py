import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Tuple

from broker.alpaca import api, get_current_price
from broker.account import get_account_equity_safe
import config
from core.executor import (
    place_order_with_trailing_stop,
    calculate_position_size_risk_based,
    get_market_exposure_factor,
    _equity_guard,
)
from signals.scoring import fetch_yfinance_stock_data, SkipSymbol


async def place_orders_concurrently(opportunities: Iterable[Tuple[str, int]]):
    """Lanza órdenes en paralelo usando asyncio y un ThreadPoolExecutor."""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        tasks = []
        equity = get_account_equity_safe()
        if not _equity_guard(equity, config._policy, "async_executor"):
            return []
        exposure = get_market_exposure_factor(config._policy)
        for symbol, score in opportunities:
            try:
                data = fetch_yfinance_stock_data(symbol)
            except SkipSymbol:
                continue
            price = data[6] if data and len(data) >= 8 else get_current_price(symbol)
            atr = data[7] if data and len(data) >= 8 else None
            sizing = calculate_position_size_risk_based(
                symbol=symbol,
                price=price,
                atr=atr,
                equity=equity,
                cfg=config._policy,
                market_exposure_factor=exposure,
            )
            if sizing["shares"] <= 0 or sizing["notional"] <= 0:
                continue
            tasks.append(
                loop.run_in_executor(
                    pool, place_order_with_trailing_stop, symbol, sizing, 1.0
                )
            )
        return await asyncio.gather(*tasks)
