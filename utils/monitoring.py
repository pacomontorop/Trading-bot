try:
    from prometheus_client import Counter, Gauge, start_http_server
except Exception:  # pragma: no cover - fallback when dependency missing
    Counter = Gauge = lambda *a, **k: None  # type: ignore

    def start_http_server(*a, **k):
        return None

orders_placed = Counter("orders_placed_total", "Total de órdenes enviadas")
open_positions_gauge = Gauge("open_positions", "Número de posiciones abiertas")


def start_metrics_server(port: int = 8001) -> None:
    if callable(start_http_server):
        start_http_server(port)


def update_positions_metric(count: int) -> None:
    if hasattr(open_positions_gauge, "set"):
        open_positions_gauge.set(count)
