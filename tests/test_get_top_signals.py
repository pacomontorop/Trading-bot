#test_get_top_signals.py

import sys
from types import SimpleNamespace
from unittest.mock import patch

# Provide dummy modules if external dependencies are missing
sys.modules.setdefault(
    "requests",
    SimpleNamespace(get=lambda *a, **k: None, adapters=SimpleNamespace(HTTPAdapter=lambda *a, **k: None)),
)
sys.modules.setdefault("requests.adapters", SimpleNamespace(HTTPAdapter=lambda *a, **k: None))
sys.modules.setdefault("dotenv", SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault(
    "yfinance",
    SimpleNamespace(Ticker=lambda *a, **k: SimpleNamespace(info={}, history=lambda *a, **k: SimpleNamespace())),
)
sys.modules.setdefault(
    "alpaca_trade_api",
    SimpleNamespace(
        REST=lambda *a, **k: SimpleNamespace(
            list_positions=lambda: [],
            get_asset=lambda s: SimpleNamespace(shortable=True),
            get_bars=lambda *a, **k: SimpleNamespace(df=SimpleNamespace(empty=True)),
            get_clock=lambda: SimpleNamespace(is_open=True),
            _session=SimpleNamespace(mount=lambda *a, **k: None),
        )
    ),
)
sys.modules.setdefault("urllib3", SimpleNamespace(util=SimpleNamespace(retry=SimpleNamespace(Retry=lambda *a, **k: None))))
sys.modules.setdefault("urllib3.util", SimpleNamespace(retry=SimpleNamespace(Retry=lambda *a, **k: None)))
sys.modules.setdefault("urllib3.util.retry", SimpleNamespace(Retry=lambda *a, **k: None))

from signals import reader
from unittest.mock import AsyncMock


def make_signals(active=True):
    return {"sig1": active, "sig2": active}


def dummy_score(signals):
    return 10 if all(signals.values()) else 0


def test_get_top_signals_returns_max_five():
    symbols = ["A", "B", "C", "D", "E", "F"]
    with patch.object(reader, "stock_assets", symbols), \
         patch("signals.reader._async_is_approved_by_quiver", new=AsyncMock(return_value=True)), \
         patch.object(reader, "is_position_open", return_value=False), \
         patch.object(reader, "get_cached_positions"), \
         patch.object(reader, "evaluated_symbols_today", set()), \
         patch.object(reader, "last_reset_date", reader.datetime.now().date()), \
         patch.object(reader, "is_blacklisted_recent_loser", return_value=False):
        results = reader.get_top_signals()

    assert len(results) == 5
    returned_symbols = {r[0] for r in results}
    assert returned_symbols.issubset(set(symbols))


def test_get_top_signals_excludes_blacklisted_symbols():
    symbols = ["A", "B", "C"]
    with patch.object(reader, "stock_assets", symbols), \
         patch("signals.reader._async_is_approved_by_quiver", new=AsyncMock(return_value=True)), \
         patch.object(reader, "is_position_open", return_value=False), \
         patch.object(reader, "get_cached_positions"), \
         patch.object(reader, "evaluated_symbols_today", set()), \
         patch.object(reader, "last_reset_date", reader.datetime.now().date()), \
         patch.object(reader, "is_blacklisted_recent_loser", side_effect=lambda s: s == "B"):
        results = reader.get_top_signals()

    returned_symbols = {r[0] for r in results}
    assert "B" not in returned_symbols
