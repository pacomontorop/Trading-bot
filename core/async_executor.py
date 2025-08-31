import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Tuple

from broker.alpaca import api
import config
from core.executor import place_order_with_trailing_stop, calculate_investment_amount


async def place_orders_concurrently(opportunities: Iterable[Tuple[str, int]]):
    """Lanza órdenes en paralelo usando asyncio y un ThreadPoolExecutor."""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        tasks = []
        equity = float(api.get_account().equity)
        for symbol, score in opportunities:
            amount = calculate_investment_amount(int(round(score)), equity, config)
            tasks.append(
                loop.run_in_executor(
                    pool, place_order_with_trailing_stop, symbol, amount, 1.0
                )
            )
        return await asyncio.gather(*tasks)
