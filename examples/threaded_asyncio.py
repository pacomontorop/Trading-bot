import asyncio
from concurrent.futures import ThreadPoolExecutor

async def limited_task(sema, idx):
    """Example coroutine that uses a semaphore."""
    async with sema:
        print(f"Inicio {idx}")
        await asyncio.sleep(1)
        print(f"Fin {idx}")
        return idx

# --- Uso de run_coroutine_threadsafe -------------------------------

def run_via_coroutine_threadsafe(loop, sema, idx):
    """Lanza la corrutina en el loop principal desde un hilo."""
    future = asyncio.run_coroutine_threadsafe(limited_task(sema, idx), loop)
    return future.result()  # Propaga resultado o excepción

async def main_threadsafe():
    loop = asyncio.get_running_loop()
    sema = asyncio.Semaphore(2)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = [executor.submit(run_via_coroutine_threadsafe, loop, sema, i)
                   for i in range(5)]
        for r in results:
            r.result()
    print("completado con run_coroutine_threadsafe")

# --- Uso de un loop independiente por hilo -------------------------

def thread_entry_new_loop(idx):
    """Cada hilo crea y gestiona su propio event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    sema = asyncio.Semaphore(2)
    loop.run_until_complete(limited_task(sema, idx))
    loop.close()

def run_with_independent_loops():
    with ThreadPoolExecutor(max_workers=3) as executor:
        for i in range(5):
            executor.submit(thread_entry_new_loop, i)
    print("completado con loops independientes")

if __name__ == "__main__":
    asyncio.run(main_threadsafe())
    run_with_independent_loops()
