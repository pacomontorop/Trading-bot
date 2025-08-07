"""Tests for signals.fmp_utils helper functions."""

import requests
from unittest.mock import patch

from signals import fmp_utils


class DummyResponse:
    """Simple stand-in for requests.Response."""

    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json


def test_get_retries_on_rate_limit():
    responses = [
        DummyResponse(429),
        DummyResponse(200, {"ok": True}),
    ]

    def fake_request(*args, **kwargs):
        return responses.pop(0)

    with patch(
        "signals.fmp_utils.throttled_request", side_effect=fake_request
    ) as req, patch("signals.fmp_utils.time.sleep") as sleep:
        result = fmp_utils._get("test-endpoint")

    assert result == {"ok": True}
    assert req.call_count == 2
    sleep.assert_called()  # ensure a delay was attempted after 429

