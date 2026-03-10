"""Lazy-initialised Alpaca REST client for the live (real-money) account.

Uses ``APCA_API_KEY_ID_REAL`` / ``APCA_API_SECRET_KEY_REAL`` environment
variables and connects to ``https://api.alpaca.markets``.

This module is intentionally separate from ``broker.alpaca`` (paper account)
so live and paper sessions can never be confused.

Activate live trading by setting ``ENABLE_LIVE_TRADING=true`` in the
environment (also requires the ``_REAL`` credential pair above).
"""

from __future__ import annotations

import os

import alpaca_trade_api as tradeapi
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils.logger import log_event
from utils.system_log import get_logger

log = get_logger("broker.alpaca_live")

_LIVE_BASE_URL = "https://api.alpaca.markets"


class _LiveLazyREST:
    """Lazy-initialised Alpaca REST proxy for the live account.

    The real ``tradeapi.REST`` instance is created on first attribute access,
    so the module can be imported without credentials (e.g. in tests).
    """

    _client: "tradeapi.REST | None" = None

    def _init_client(self) -> "tradeapi.REST":
        if self._client is not None:
            return self._client

        key_id = os.getenv("APCA_API_KEY_ID_REAL")
        secret = os.getenv("APCA_API_SECRET_KEY_REAL")
        if not key_id or not secret:
            raise RuntimeError(
                "Live Alpaca credentials not configured: "
                "set APCA_API_KEY_ID_REAL and APCA_API_SECRET_KEY_REAL"
            )

        client = tradeapi.REST(key_id, secret, _LIVE_BASE_URL, api_version="v2")
        retry = Retry(total=3, backoff_factor=3)
        adapter = HTTPAdapter(max_retries=retry)
        client._session.mount("https://", adapter)
        client._session.mount("http://", adapter)

        try:  # pragma: no cover - network dependent
            acct = client.get_account()
            log.info(
                f"[ALPACA_LIVE] connected base_url={_LIVE_BASE_URL} "
                f"account_id={getattr(acct, 'id', 'unknown')} "
                f"buying_power={getattr(acct, 'buying_power', '0')}"
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            log.warning(f"[ALPACA_LIVE] unable to fetch account details: {exc}")

        self._client = client
        return client

    def __getattr__(self, name: str):  # noqa: ANN001
        return getattr(self._init_client(), name)


live_api = _LiveLazyREST()


def is_live_configured() -> bool:
    """Return True if live account credentials are present in the environment."""
    return bool(
        os.getenv("APCA_API_KEY_ID_REAL") and os.getenv("APCA_API_SECRET_KEY_REAL")
    )


def is_live_enabled() -> bool:
    """Return True if live trading is both configured and enabled via env flag."""
    flag = os.getenv("ENABLE_LIVE_TRADING", "false").strip().lower()
    return flag in {"1", "true", "yes", "on"} and is_live_configured()


def list_live_positions():
    """Return current open positions for the live account."""
    try:  # pragma: no cover - network call
        return live_api.list_positions()
    except Exception:
        return []


def list_live_open_orders():
    """Return active orders for the live account.

    Fetches all of today's orders and filters to active statuses so that
    bracket stop legs (which have status ``held``, not ``open``) are included.
    Without ``held`` orders the trailing-stop logic cannot find the bracket's
    stop leg and mistakenly tries to submit a duplicate sell order, which
    Alpaca rejects with ``insufficient qty available``.
    """
    import datetime

    try:  # pragma: no cover - network call
        today = datetime.date.today().isoformat()
        all_orders = live_api.list_orders(status="all", limit=500, after=f"{today}T00:00:00Z")
        active = {"new", "accepted", "held", "pending_new", "accepted_for_bidding", "partially_filled"}
        return [o for o in (all_orders or []) if str(getattr(o, "status", "")).lower() in active]
    except Exception:
        return []
