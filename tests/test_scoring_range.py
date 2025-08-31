import os, sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from signals import scoring


def test_score_is_normalized(monkeypatch):
    assert isinstance(scoring._normalize_0_100(1234.56), int)
    assert scoring._normalize_0_100(1234.56) == 100
    assert scoring._normalize_0_100(-999) == 0
