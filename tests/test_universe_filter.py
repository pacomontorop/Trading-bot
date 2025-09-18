import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

from signals.reader import is_symbol_excluded


def test_universe_exclusion_patterns():
    symbols = ["HFRO.PRA", "WLACU", "XYZW", "NE.WSA"]
    for symbol in symbols:
        assert is_symbol_excluded(symbol)
