# quiver_approval.py

"""Convenient re-exports for Quiver utilities."""

from . import quiver_utils as _quiver

__all__ = [
    "is_approved_by_quiver",
    "evaluate_quiver_signals",
    "get_all_quiver_signals",
]


def is_approved_by_quiver(symbol: str) -> dict:
    """Proxy to :func:`quiver_utils.is_approved_by_quiver`."""

    return _quiver.is_approved_by_quiver(symbol)


def evaluate_quiver_signals(signals: dict, symbol: str = "") -> dict:
    """Proxy to :func:`quiver_utils.evaluate_quiver_signals`."""

    return _quiver.evaluate_quiver_signals(signals, symbol)


def get_all_quiver_signals(symbol: str) -> dict:
    """Proxy to :func:`quiver_utils.get_all_quiver_signals`."""

    return _quiver.get_all_quiver_signals(symbol)


def __getattr__(name):
    """Delegate attribute access to ``quiver_utils``.

    Using ``__getattr__`` allows test suites to patch objects on
    ``signals.quiver_utils`` and have those patches reflected here.
    """

    return getattr(_quiver, name)
