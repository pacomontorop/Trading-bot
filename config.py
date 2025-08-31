import os
import yaml

POLICY_PATH = os.path.join(os.path.dirname(__file__), "config", "policy.yaml")
try:
    with open(POLICY_PATH, "r", encoding="utf-8") as f:
        _policy = yaml.safe_load(f) or {}
except Exception:
    _policy = {}

gate_cfg = _policy.get("gate", {})
LIQ_MIN_MKTCAP = float(gate_cfg.get("min_cap", 500_000_000))
LIQ_MIN_AVG_VOL20 = float(gate_cfg.get("min_avg_vol_20d", 500_000))
MIN_PRICE = float(gate_cfg.get("min_price", 3.0))
STRONG_SIGNAL_MAX_AGE_DAYS = int(gate_cfg.get("strong_signal_max_age_days", 3))

SCAN_WINDOW_MINUTES = 45
MAX_TRADES_PER_DAY = 5
STRATEGY_VER = "long_v2"
ALLOW_MEDIUM_SIGNALS = True
USE_REDIS = bool(os.getenv("REDIS_URL"))
