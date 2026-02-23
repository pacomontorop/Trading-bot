"""Pytest configuration and shared fixtures for the Trading-bot test suite.

This conftest sets dummy Alpaca credentials in the environment *before* any
test module is imported, so that ``broker.alpaca`` (which uses lazy
initialisation) can be imported without raising a ``ValueError`` about missing
API keys.  Real network calls are still prevented by the lazy proxy — they
only happen when a test explicitly triggers a broker method.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Set dummy broker credentials so imports succeed in the test environment.
# These values are never sent to Alpaca because broker.api uses lazy init
# and tests mock the methods they need.
# ---------------------------------------------------------------------------
os.environ.setdefault("APCA_API_KEY_ID", "PKTEST00000000000000")
os.environ.setdefault("APCA_API_SECRET_KEY", "test_secret_key_placeholder_00000000000000")
os.environ.setdefault("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
