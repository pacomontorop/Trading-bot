import os
import sys

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

# Ensure project root is on sys.path for module resolution
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.orders import resolve_time_in_force


def test_resolve_time_in_force_crypto_fractional():
    assert resolve_time_in_force(0.5, asset_class="crypto") == "gtc"


def test_resolve_time_in_force_equity_fractional():
    assert resolve_time_in_force(0.5, asset_class="us_equity") == "day"
