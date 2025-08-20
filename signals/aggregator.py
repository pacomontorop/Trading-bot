from datetime import datetime
import math
from typing import Dict, Any


class WeightedSignalAggregator:
    """Combina señales externas asignando pesos a cada una.

    Cada señal puede ser un ``float`` simple o un ``dict`` con las claves
    ``score`` y ``timestamp``.  Cuando se proporciona una marca de tiempo se
    aplica un factor de decaimiento exponencial para privilegiar señales más
    recientes.
    """

    def __init__(self, weights: Dict[str, float] | None = None, decay: float = 0.1):
        self.weights = weights or {}
        self.decay = decay

    def _age_factor(self, ts: datetime | None) -> float:
        if ts is None:
            return 1.0
        age_days = (datetime.utcnow() - ts).total_seconds() / 86400
        if age_days <= 0:
            return 1.0
        return math.exp(-self.decay * age_days)

    def combine(self, signals: Dict[str, Any]) -> float:
        total = 0.0
        weight_sum = 0.0
        for name, value in signals.items():
            if value is None:
                continue
            w = self.weights.get(name, 1.0)
            if isinstance(value, dict):
                score = value.get("score", 0.0)
                ts = value.get("timestamp")
                w *= self._age_factor(ts)
            else:
                score = value
                ts = None
            total += score * w
            weight_sum += w
        return total / weight_sum if weight_sum else 0.0
