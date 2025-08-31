import sys
from types import SimpleNamespace
import pandas as pd
import importlib.util


def test_has_downtrend_handles_multiindex(monkeypatch):
    monkeypatch.setitem(sys.modules, 'requests', SimpleNamespace(get=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, 'dotenv', SimpleNamespace(load_dotenv=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, 'alpaca_trade_api', SimpleNamespace(REST=lambda *a, **k: SimpleNamespace(list_positions=lambda: [], get_asset=lambda s: SimpleNamespace(options_enabled=True))))
    monkeypatch.setitem(sys.modules, 'urllib3', SimpleNamespace(util=SimpleNamespace(retry=SimpleNamespace(Retry=lambda *a, **k: None))))
    monkeypatch.setitem(sys.modules, 'urllib3.util', SimpleNamespace(retry=SimpleNamespace(Retry=lambda *a, **k: None)))
    monkeypatch.setitem(sys.modules, 'urllib3.util.retry', SimpleNamespace(Retry=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, 'signals.quiver_utils', SimpleNamespace(_async_is_approved_by_quiver=lambda *a, **k: True, fetch_quiver_signals=lambda *a, **k: [], is_approved_by_quiver=lambda *a, **k: {"active_signals": []}))
    monkeypatch.setitem(sys.modules, 'signals.quiver_event_loop', SimpleNamespace(run_in_quiver_loop=lambda coro: None))
    monkeypatch.setitem(sys.modules, 'signals.scoring', SimpleNamespace(fetch_yfinance_stock_data=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, 'utils.logger', SimpleNamespace(log_event=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, 'signals.adaptive_bonus', SimpleNamespace(apply_adaptive_bonus=lambda *a, **k: 0))
    monkeypatch.setitem(sys.modules, 'signals.filters', SimpleNamespace(
        is_position_open=lambda *a, **k: False,
        is_approved_by_finnhub_and_alphavantage=lambda *a, **k: True,
        get_cached_positions=lambda *a, **k: [],
        is_approved_by_quiver=lambda *a, **k: True,
    ))
    monkeypatch.setitem(sys.modules, 'broker.alpaca', SimpleNamespace(api=None))
    monkeypatch.setitem(sys.modules, 'yfinance', SimpleNamespace(download=lambda *a, **k: None))

    spec = importlib.util.spec_from_file_location('tmp_reader', 'signals/reader.py')
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)

    idx = pd.date_range(end=pd.Timestamp.today(), periods=4)
    data = {('Close', 'BRK.B'): [100, 99, 98, 97]}
    df = pd.DataFrame(data, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)

    def fake_download(symbol, period, interval, progress):
        assert symbol == 'BRK.B'
        return df

    monkeypatch.setattr(reader.yf, 'download', fake_download)
    assert reader.has_downtrend('BRK.B') is True
