import requests
from unittest.mock import patch

from signals import quiver_utils


class DummyResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._json


def test_safe_quiver_request_retries_on_rate_limit():
    responses = [
        DummyResponse(429),
        DummyResponse(429),
        DummyResponse(200, {"ok": True}),
    ]

    def fake_request(*args, **kwargs):
        return responses.pop(0)

    with patch("signals.quiver_utils.throttled_request", side_effect=fake_request) as req, \
         patch("signals.quiver_utils.time.sleep") as sleep:
        result = quiver_utils.safe_quiver_request("test-url")

    assert result == {"ok": True}
    assert req.call_count == 3
    assert sleep.call_count == 2
