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


def test_get_retries_on_exception():
    responses = [
        requests.ConnectionError("boom"),
        DummyResponse(200, {"ok": True}),
    ]

    def fake_request(*args, **kwargs):
        resp = responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    with patch(
        "signals.fmp_utils.throttled_request", side_effect=fake_request
    ) as req, patch("signals.fmp_utils.time.sleep") as sleep:
        result = fmp_utils._get("test-endpoint")

    assert result == {"ok": True}
    assert req.call_count == 2
    sleep.assert_called()


def test_company_profile_wrapper_calls_get():
    with patch("signals.fmp_utils._get") as get_mock:
        fmp_utils.company_profile("AAPL")
    get_mock.assert_called_once_with("profile/AAPL")


def test_quote_wrapper_calls_get():
    with patch("signals.fmp_utils._get") as get_mock:
        fmp_utils.quote("AAPL")
    get_mock.assert_called_once_with("quote/AAPL")


def test_financial_ratios_wrapper_calls_get():
    with patch("signals.fmp_utils._get") as get_mock:
        fmp_utils.financial_ratios("AAPL", period="quarter", limit=10)
    get_mock.assert_called_once_with(
        "ratios/AAPL", {"period": "quarter", "limit": 10}
    )


def test_key_metrics_wrapper_calls_get():
    with patch("signals.fmp_utils._get") as get_mock:
        fmp_utils.key_metrics("AAPL", period="annual", limit=5)
    get_mock.assert_called_once_with(
        "key-metrics/AAPL", {"period": "annual", "limit": 5}
    )


def test_grades_news_calls_grade_endpoint():
    """grades_news should query the `grade` endpoint with the symbol in the path."""
    with patch("signals.fmp_utils._get") as get_mock:
        fmp_utils.grades_news("AAPL", page=2, limit=3)
    get_mock.assert_called_once_with("grade/AAPL", {"page": 2, "limit": 3})

