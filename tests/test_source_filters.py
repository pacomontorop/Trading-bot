import os
from unittest.mock import patch

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

from signals.filters import is_symbol_approved


def test_macro_penalty_can_be_overcome():
    with patch('signals.filters.macro_score', return_value=-0.5), \
         patch('signals.filters.volatility_penalty', return_value=0.0), \
         patch('signals.filters.reddit_score', return_value=0.2), \
         patch('signals.filters.is_approved_by_quiver', return_value=True):
        assert is_symbol_approved('AAPL') is True


def test_rejects_when_score_negative():
    with patch('signals.filters.macro_score', return_value=-1.0), \
         patch('signals.filters.volatility_penalty', return_value=0.3), \
         patch('signals.filters.reddit_score', return_value=-0.2), \
         patch('signals.filters.is_approved_by_quiver', return_value=False), \
        patch('signals.filters.is_approved_by_finnhub_and_alphavantage', return_value=False), \
        patch('signals.filters.is_approved_by_fmp', return_value=False):
        assert is_symbol_approved('AAPL') is False


def test_requires_external_approval():
    """Even with a positive score, lack of external approval should reject."""
    with patch('signals.filters.macro_score', return_value=0.2), \
         patch('signals.filters.volatility_penalty', return_value=0.1), \
         patch('signals.filters.reddit_score', return_value=0.2), \
         patch('signals.filters.is_approved_by_quiver', return_value=False), \
         patch('signals.filters.is_approved_by_finnhub_and_alphavantage', return_value=False), \
         patch('signals.filters.is_approved_by_fmp', return_value=False):
        assert is_symbol_approved('AAPL') is False


def test_approves_when_score_positive():
    with patch('signals.filters.macro_score', return_value=0.2), \
         patch('signals.filters.volatility_penalty', return_value=0.1), \
         patch('signals.filters.reddit_score', return_value=0.3), \
         patch('signals.filters.is_approved_by_quiver', return_value=True):
        assert is_symbol_approved('AAPL') is True

