_cached = {}

def get_cached_market_regime():
    # placeholder: devolver None u opciones "normal", "elevated_vol", "high_vol"
    return _cached.get("market_regime")

def set_cached_market_regime(value: str):
    _cached["market_regime"] = value
