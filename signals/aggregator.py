from typing import Dict

class WeightedSignalAggregator:
    """Combina señales externas asignando pesos a cada una."""
    def __init__(self, weights: Dict[str, float] | None = None):
        self.weights = weights or {}

    def combine(self, signals: Dict[str, float]) -> float:
        total = 0.0
        weight_sum = 0.0
        for name, value in signals.items():
            if value is None:
                continue
            w = self.weights.get(name, 1.0)
            total += value * w
            weight_sum += w
        return total / weight_sum if weight_sum else 0.0
