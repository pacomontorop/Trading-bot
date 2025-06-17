"""Convenient re-exports for Quiver utilities."""

from . import quiver_utils as _quiver

QUIVER_APPROVAL_THRESHOLD = _quiver.QUIVER_APPROVAL_THRESHOLD

__all__ = [
    "is_approved_by_quiver",
    "evaluate_quiver_signals",
    "get_all_quiver_signals",
    "QUIVER_APPROVAL_THRESHOLD",
]


def __getattr__(name):
    """Delegate attribute access to ``quiver_utils``.

    Using ``__getattr__`` allows test suites to patch objects on
    ``signals.quiver_utils`` and have those patches reflected here.
    """
    return getattr(_quiver, name)
