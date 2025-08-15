import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Tuple

from core.executor import place_order_with_trailing_stop, calculate_investment_amount


async def place_orders_concurrently(opportunities: Iterable[Tuple[str, int]]):
    """Lanza órdenes en paralelo usando asyncio y un ThreadPoolExecutor."""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        tasks = []
        for symbol, score in opportunities:
            amount = calculate_investment_amount(score, symbol=symbol)
            tasks.append(
                loop.run_in_executor(
                    pool, place_order_with_trailing_stop, symbol, amount, 1.0
                )
            )
        return await asyncio.gather(*tasks)
