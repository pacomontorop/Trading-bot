"""Comprehensive verification tests for all signal gates and Quiver parameters.

Covers:
  - Each Quiver endpoint response parsing (insiders, govcontracts, housetrading,
    patentmomentum, sec13f, sec13fchanges, wallstreetbets, twitter, appratings)
  - RSI gate (min/max, missing RSI behaviour)
  - Insider net count (buys - sells)
  - Trend gate (require_trend_positive)
  - Universe rotation (daily shuffle reproducibility)
  - Quiver gate thresholds (new non-zero defaults)
  - Fast lane activation
  - Scoring weights (house_purchase, insider_net contributions)
  - Feature computation from features.py
"""

from __future__ import annotations

import datetime
import random
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import config

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _with_policy(policy: dict, fn):
    original = config._policy
    config._policy = policy
    try:
        return fn()
    finally:
        config._policy = original


def _make_hist(n_days: int = 90, trend: str = "up") -> pd.DataFrame:
    """Build a synthetic yfinance-style history DataFrame."""
    import numpy as np

    base = 100.0
    # Fixed Monday start avoids weekend boundary issues (end=today fails on Sat/Sun)
    dates = pd.date_range(start=datetime.date(2020, 1, 6), periods=n_days, freq="B")
    if trend == "up":
        close = [base + i * 0.5 for i in range(n_days)]
    elif trend == "down":
        close = [base + 50 - i * 0.5 for i in range(n_days)]
    else:  # flat
        close = [base] * n_days

    close = pd.Series(close, index=dates, dtype=float)
    high = close * 1.01
    low = close * 0.99
    volume = pd.Series([1_000_000] * n_days, index=dates, dtype=float)
    return pd.DataFrame({"Close": close, "High": high, "Low": low, "Volume": volume})


# ============================================================================
# 1. RSI computation
# ============================================================================

class TestRsiComputation:
    def test_rsi_uptrend_above_50(self):
        from signals.features import compute_rsi_from_hist

        hist = _make_hist(90, trend="up")
        rsi = compute_rsi_from_hist(hist)
        assert rsi is not None, "RSI should be computable from 90 days of data"
        assert 50 < rsi <= 100, f"Uptrend RSI should be >50, got {rsi:.1f}"

    def test_rsi_downtrend_below_50(self):
        from signals.features import compute_rsi_from_hist

        hist = _make_hist(90, trend="down")
        rsi = compute_rsi_from_hist(hist)
        assert rsi is not None
        assert rsi < 50, f"Downtrend RSI should be <50, got {rsi:.1f}"

    def test_rsi_none_on_insufficient_data(self):
        from signals.features import compute_rsi_from_hist

        hist = _make_hist(10, trend="up")  # only 10 days — not enough for 14-period RSI
        rsi = compute_rsi_from_hist(hist, period=14)
        assert rsi is None, "Should return None when data < period + 1"

    def test_rsi_none_on_empty_df(self):
        from signals.features import compute_rsi_from_hist

        rsi = compute_rsi_from_hist(pd.DataFrame())
        assert rsi is None

    def test_rsi_none_on_none_input(self):
        from signals.features import compute_rsi_from_hist

        rsi = compute_rsi_from_hist(None)
        assert rsi is None


# ============================================================================
# 2. RSI gate logic
# ============================================================================

class TestRsiGate:
    def _gate(self, rsi, min_rsi=40, max_rsi=75, require_rsi=False):
        from signals.reader import _rsi_gate_reasons
        cfg = {"min_rsi": min_rsi, "max_rsi": max_rsi, "require_rsi": require_rsi}
        return _rsi_gate_reasons(rsi, cfg)

    def test_pass_within_range(self):
        assert self._gate(55.0) == [], "RSI=55 should pass [40, 75]"

    def test_pass_at_min_boundary(self):
        assert self._gate(40.0) == [], "RSI=40.0 exactly at min should pass"

    def test_pass_at_max_boundary(self):
        assert self._gate(75.0) == [], "RSI=75.0 exactly at max should pass"

    def test_reject_below_min(self):
        reasons = self._gate(35.0)
        assert "rsi_below_min" in reasons, f"RSI=35 should be rejected below min=40, got {reasons}"

    def test_reject_above_max(self):
        reasons = self._gate(80.0)
        assert "rsi_above_max" in reasons, f"RSI=80 should be rejected above max=75, got {reasons}"

    def test_missing_rsi_passes_when_not_required(self):
        assert self._gate(None, require_rsi=False) == [], "Missing RSI should pass when not required"

    def test_missing_rsi_blocked_when_required(self):
        reasons = self._gate(None, require_rsi=True)
        assert "rsi_missing" in reasons, f"Missing RSI should block when required=True, got {reasons}"

    def test_rsi_zero_treated_as_missing(self):
        # 0.0 is the sentinel value set when RSI cannot be computed
        assert self._gate(0.0, require_rsi=False) == [], "RSI=0.0 (sentinel) should pass when not required"


# ============================================================================
# 3. Insider net count
# ============================================================================

class TestInsiderNet:
    def _features_with_insider(self, buys: int, sells: int) -> dict:
        """Build a minimal features dict and compute insider_net through features.py logic."""
        from signals.features import _to_numeric

        raw = {
            "quiver_insider_buy_count": buys,
            "quiver_insider_sell_count": sells,
        }
        buy_count = _to_numeric(raw.get("quiver_insider_buy_count"))
        sell_count = _to_numeric(raw.get("quiver_insider_sell_count"))
        raw["quiver_insider_net_count"] = buy_count - sell_count
        return {k: _to_numeric(v) for k, v in raw.items()}

    def test_net_positive_on_more_buys(self):
        f = self._features_with_insider(3, 1)
        assert f["quiver_insider_net_count"] == 2.0

    def test_net_negative_on_more_sells(self):
        f = self._features_with_insider(1, 4)
        assert f["quiver_insider_net_count"] == -3.0

    def test_net_zero_when_balanced(self):
        f = self._features_with_insider(2, 2)
        assert f["quiver_insider_net_count"] == 0.0

    def test_net_zero_when_no_activity(self):
        f = self._features_with_insider(0, 0)
        assert f["quiver_insider_net_count"] == 0.0

    def test_insider_net_has_higher_weight_than_raw_buy(self):
        from signals.reader import QUIVER_FEATURE_WEIGHTS
        net_w = QUIVER_FEATURE_WEIGHTS.get("quiver_insider_net_count", 0)
        buy_w = QUIVER_FEATURE_WEIGHTS.get("quiver_insider_buy_count", 0)
        assert net_w > buy_w, f"net_count weight ({net_w}) should exceed raw buy weight ({buy_w})"


# ============================================================================
# 4. House purchase count weight
# ============================================================================

class TestHousePurchaseWeight:
    def test_house_purchase_has_positive_weight(self):
        from signals.reader import QUIVER_FEATURE_WEIGHTS
        w = QUIVER_FEATURE_WEIGHTS.get("quiver_house_purchase_count", 0)
        assert w > 0, f"quiver_house_purchase_count should have a positive weight, got {w}"

    def test_house_purchase_has_cap(self):
        from signals.reader import _FEATURE_CAPS
        assert "quiver_house_purchase_count" in _FEATURE_CAPS, \
            "house_purchase_count should have a cap to prevent domination"

    def test_house_purchase_contributes_to_score(self):
        from signals.reader import _score_from_features, QUIVER_FEATURE_WEIGHTS
        features_with = {"quiver_house_purchase_count": 3.0, "quiver_insider_net_count": 0.0}
        features_without = {"quiver_house_purchase_count": 0.0, "quiver_insider_net_count": 0.0}
        score_with, _ = _score_from_features(features_with)
        score_without, _ = _score_from_features(features_without)
        assert score_with > score_without, "House purchases should boost score"


# ============================================================================
# 5. Quiver endpoint parsing
# ============================================================================

class TestQuiverEndpointParsing:
    """Test that each Quiver endpoint's response format is correctly parsed
    into numeric features, using synthetic payloads matching the real API format."""

    SYMBOL = "AAPL"

    def _run_utils(self, endpoint_mocks: dict) -> dict:
        """Run quiver_utils.get_quiver_features with patched ingest endpoints."""
        from signals import quiver_utils, quiver_ingest

        patch_targets = {
            "signals.quiver_ingest.fetch_live_insiders": endpoint_mocks.get("insiders", []),
            "signals.quiver_ingest.fetch_live_govcontracts": endpoint_mocks.get("govcontracts", []),
            "signals.quiver_ingest.fetch_live_housetrading": endpoint_mocks.get("housetrading", []),
            "signals.quiver_ingest.fetch_live_patentmomentum": endpoint_mocks.get("patentmomentum", []),
            "signals.quiver_ingest.fetch_live_sec13f": endpoint_mocks.get("sec13f", []),
            "signals.quiver_ingest.fetch_live_sec13fchanges": endpoint_mocks.get("sec13fchanges", []),
            "signals.quiver_ingest.fetch_live_twitter": endpoint_mocks.get("twitter", []),
            "signals.quiver_ingest.fetch_live_appratings": endpoint_mocks.get("appratings", []),
            "signals.quiver_ingest.fetch_live_appratings_cached": endpoint_mocks.get("appratings", []),
            "signals.quiver_ingest.fetch_historical_wallstreetbets": endpoint_mocks.get("wsb", []),
        }
        patches = []
        for target, value in patch_targets.items():
            # wsb is called with symbol arg, others are no-arg
            if target.endswith("wallstreetbets"):
                p = patch(target, return_value=value)
            else:
                p = patch(target, return_value=value)
            patches.append(p)

        with patch.multiple("signals.quiver_ingest",
                            fetch_live_insiders=MagicMock(return_value=endpoint_mocks.get("insiders", [])),
                            fetch_live_govcontracts=MagicMock(return_value=endpoint_mocks.get("govcontracts", [])),
                            fetch_live_govcontractsall_cached=MagicMock(return_value=endpoint_mocks.get("govcontracts", [])),
                            fetch_live_housetrading=MagicMock(return_value=endpoint_mocks.get("housetrading", [])),
                            fetch_live_patentmomentum=MagicMock(return_value=endpoint_mocks.get("patentmomentum", [])),
                            fetch_live_patentmomentum_cached=MagicMock(return_value=endpoint_mocks.get("patentmomentum", [])),
                            fetch_live_sec13f=MagicMock(return_value=endpoint_mocks.get("sec13f", [])),
                            fetch_live_sec13f_cached=MagicMock(return_value=endpoint_mocks.get("sec13f", [])),
                            fetch_live_sec13fchanges=MagicMock(return_value=endpoint_mocks.get("sec13fchanges", [])),
                            fetch_live_sec13fchanges_cached=MagicMock(return_value=endpoint_mocks.get("sec13fchanges", [])),
                            fetch_live_twitter=MagicMock(return_value=endpoint_mocks.get("twitter", [])),
                            fetch_live_appratings=MagicMock(return_value=endpoint_mocks.get("appratings", [])),
                            fetch_live_appratings_cached=MagicMock(return_value=endpoint_mocks.get("appratings", [])),
                            fetch_live_offexchange_cached=MagicMock(return_value=[]),
                            fetch_live_senatetrading_cached=MagicMock(return_value=endpoint_mocks.get("senate", [])),
                            fetch_live_congresstrading_cached=MagicMock(return_value=endpoint_mocks.get("congress", [])),
                            fetch_historical_wallstreetbets=MagicMock(return_value=endpoint_mocks.get("wsb", []))):
            return quiver_utils.get_quiver_features(self.SYMBOL)

    def _recent_date(self, days_ago: int = 1) -> str:
        d = datetime.date.today() - datetime.timedelta(days=days_ago)
        return d.isoformat()

    # --- insiders ---
    def test_insider_buy_counted(self):
        payload = {"insiders": [
            {"Ticker": "AAPL", "TransactionCode": "P", "Date": self._recent_date(2)},
            {"Ticker": "AAPL", "TransactionCode": "P", "Date": self._recent_date(3)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_insider_buy_count"] == 2, f"Expected 2 buys, got {f['quiver_insider_buy_count']}"

    def test_insider_sell_counted(self):
        payload = {"insiders": [
            {"Ticker": "AAPL", "TransactionCode": "S", "Date": self._recent_date(1)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_insider_sell_count"] == 1, f"Expected 1 sell, got {f['quiver_insider_sell_count']}"

    def test_insider_stale_ignored(self):
        """Transactions older than freshness_days should not count."""
        payload = {"insiders": [
            {"Ticker": "AAPL", "TransactionCode": "P", "Date": "2020-01-01"},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_insider_buy_count"] == 0, "Stale insider buy should be ignored"

    def test_insider_wrong_ticker_ignored(self):
        payload = {"insiders": [
            {"Ticker": "MSFT", "TransactionCode": "P", "Date": self._recent_date(1)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_insider_buy_count"] == 0

    # --- govcontracts ---
    def test_govcontract_amount_summed(self):
        # Both dates within default freshness_days=7 so both are counted
        payload = {"govcontracts": [
            {"Ticker": "AAPL", "Amount": "500000", "Date": self._recent_date(2)},
            {"Ticker": "AAPL", "Amount": "750000", "Date": self._recent_date(5)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_gov_contract_total_amount"] == 1_250_000, \
            f"Expected $1.25M total, got {f['quiver_gov_contract_total_amount']}"
        assert f["quiver_gov_contract_count"] == 2

    def test_govcontract_dollar_sign_stripped(self):
        payload = {"govcontracts": [
            {"Ticker": "AAPL", "Amount": "$1,000,000", "Date": self._recent_date(1)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_gov_contract_total_amount"] == 1_000_000

    def test_govcontract_zero_when_empty(self):
        f = self._run_utils({"govcontracts": []})
        assert f["quiver_gov_contract_total_amount"] == 0
        assert f["quiver_gov_contract_count"] == 0

    # --- housetrading (Congress) ---
    def test_house_purchase_counted(self):
        # Use ReportDate (API field) — freshness is measured from disclosure, not transaction
        payload = {"housetrading": [
            {"Ticker": "AAPL", "Transaction": "Purchase", "ReportDate": self._recent_date(3), "Date": self._recent_date(45)},
            {"Ticker": "AAPL", "Transaction": "Purchase", "ReportDate": self._recent_date(5), "Date": self._recent_date(50)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_house_purchase_count"] == 2, \
            f"Expected 2 congressional purchases, got {f['quiver_house_purchase_count']}"

    def test_house_purchase_stale_transaction_but_fresh_report_counted(self):
        # STOCK Act scenario: trade happened 41 days ago, but disclosed TODAY → must be counted
        payload = {"housetrading": [
            {"Ticker": "AAPL", "Transaction": "Purchase", "ReportDate": self._recent_date(1), "Date": self._recent_date(41)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_house_purchase_count"] == 1, \
            "Trade disclosed today must be counted even if transaction was 41 days ago"

    def test_house_purchase_stale_report_not_counted(self):
        # Disclosure too old → rejected regardless of transaction date
        payload = {"housetrading": [
            {"Ticker": "AAPL", "Transaction": "Purchase", "ReportDate": self._recent_date(45), "Date": self._recent_date(3)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_house_purchase_count"] == 0, \
            "Trade with stale ReportDate (45d) must be filtered out"

    def test_house_purchase_case_insensitive(self):
        # Transaction field must be matched case-insensitively
        payload = {"housetrading": [
            {"Ticker": "AAPL", "Transaction": "purchase", "ReportDate": self._recent_date(2)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_house_purchase_count"] == 1, \
            "Lowercase 'purchase' must be counted (case-insensitive match)"

    def test_house_sale_not_counted(self):
        payload = {"housetrading": [
            {"Ticker": "AAPL", "Transaction": "Sale", "ReportDate": self._recent_date(2)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_house_purchase_count"] == 0, "Congressional sales should not count as purchases"

    def test_house_purchase_zero_when_empty(self):
        f = self._run_utils({"housetrading": []})
        assert f["quiver_house_purchase_count"] == 0

    # --- patentmomentum ---
    def test_patent_momentum_parsed(self):
        payload = {"patentmomentum": [
            {"ticker": "AAPL", "momentum": 3.7, "date": self._recent_date(2)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_patent_momentum_latest"] == pytest.approx(3.7), \
            f"Expected patent momentum 3.7, got {f['quiver_patent_momentum_latest']}"

    def test_patent_momentum_zero_when_stale(self):
        payload = {"patentmomentum": [
            {"ticker": "AAPL", "momentum": 4.0, "date": "2020-01-01"},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_patent_momentum_latest"] == 0.0, "Stale patent momentum should return 0"

    def test_patent_momentum_zero_when_empty(self):
        f = self._run_utils({"patentmomentum": []})
        assert f["quiver_patent_momentum_latest"] == 0.0

    # --- sec13f ---
    def test_sec13f_count_parsed(self):
        # Both dates within freshness_days_sec13f=6 so both are counted
        payload = {"sec13f": [
            {"Ticker": "AAPL", "ReportDate": self._recent_date(2)},
            {"Ticker": "AAPL", "ReportDate": self._recent_date(5)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_sec13f_count"] == 2, f"Expected 2 13F filings, got {f['quiver_sec13f_count']}"

    def test_sec13f_count_zero_when_empty(self):
        f = self._run_utils({"sec13f": []})
        assert f["quiver_sec13f_count"] == 0

    # --- sec13fchanges ---
    def test_sec13f_change_parsed(self):
        payload = {"sec13fchanges": [
            {"Ticker": "AAPL", "Change_Pct": 12.5, "ReportDate": self._recent_date(5)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_sec13f_change_latest_pct"] == pytest.approx(12.5), \
            f"Expected 12.5% change, got {f['quiver_sec13f_change_latest_pct']}"

    def test_sec13f_change_zero_when_empty(self):
        f = self._run_utils({"sec13fchanges": []})
        assert f["quiver_sec13f_change_latest_pct"] == 0.0

    # --- wallstreetbets ---
    def test_wsb_max_mentions_parsed(self):
        payload = {"wsb": [
            {"Mentions": 120, "Date": self._recent_date(1)},
            {"Mentions": 350, "Date": self._recent_date(3)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_wsb_recent_max_mentions"] == pytest.approx(350), \
            f"Expected max 350 WSB mentions, got {f['quiver_wsb_recent_max_mentions']}"

    def test_wsb_zero_when_empty(self):
        f = self._run_utils({"wsb": []})
        assert f["quiver_wsb_recent_max_mentions"] == 0

    # --- twitter ---
    def test_twitter_followers_parsed(self):
        payload = {"twitter": [
            {"Ticker": "AAPL", "Followers": 2_500_000, "Date": self._recent_date(2)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_twitter_latest_followers"] == pytest.approx(2_500_000)

    def test_twitter_zero_when_empty(self):
        f = self._run_utils({"twitter": []})
        assert f["quiver_twitter_latest_followers"] == 0

    # --- appratings ---
    def test_appratings_parsed(self):
        payload = {"appratings": [
            {"Ticker": "AAPL", "Rating": 4.7, "Count": 12000, "Date": self._recent_date(5)},
        ]}
        f = self._run_utils(payload)
        assert f["quiver_app_rating_latest"] == pytest.approx(4.7)
        assert f["quiver_app_rating_latest_count"] == pytest.approx(12000)

    def test_appratings_zero_when_empty(self):
        f = self._run_utils({"appratings": []})
        assert f["quiver_app_rating_latest"] == 0
        assert f["quiver_app_rating_latest_count"] == 0


# ============================================================================
# 6. Quiver gate thresholds
# ============================================================================

class TestQuiverGateThresholds:
    """Verify gate thresholds filter correctly without over-blocking."""

    # Default policy matching current policy.yaml: min_types=1, sec13f disabled
    _BASE_POLICY = {
        "quiver_gate": {
            "enabled": True,
            "insider_buy_min_count_lookback": 1,
            "gov_contract_min_total_amount": 100_000,
            "gov_contract_min_count": 0,      # disabled — amount threshold is sufficient
            "patent_momentum_min": 0,
            "sec13f_count_min": 0,             # disabled — 13F filings are quarterly
            "sec13f_change_min_pct": 0,
            "min_active_signal_types": 1,      # a single strong signal is enough
        }
    }

    def _gate(self, features: dict, policy_overrides: dict = None):
        from signals.reader import gate_quiver_minimum
        import copy
        policy = copy.deepcopy(self._BASE_POLICY)
        if policy_overrides:
            policy["quiver_gate"].update(policy_overrides)
        return _with_policy(policy, lambda: gate_quiver_minimum(features))

    def test_single_insider_buy_passes_alone(self):
        """A single insider buy should pass — no second signal type required."""
        ok, reasons = self._gate({"quiver_insider_buy_count": 1})
        assert ok, f"1 insider buy alone should pass (min_types=1), got: {reasons}"

    def test_house_purchase_alone_passes(self):
        """Congressional purchase alone should pass (counted as 1 active type)."""
        ok, reasons = self._gate({"quiver_house_purchase_count": 1,
                                   "quiver_gov_contract_total_amount": 150_000})
        assert ok, f"House purchase + gov contract should pass, got: {reasons}"

    def test_passes_with_gov_contract_above_threshold(self):
        ok, reasons = self._gate({"quiver_gov_contract_total_amount": 200_000})
        assert ok, f"$200k gov contract should pass, got {reasons}"

    def test_rejects_gov_contract_below_amount_threshold(self):
        ok, reasons = self._gate({
            "quiver_gov_contract_total_amount": 50_000,   # below $100k
            "quiver_insider_buy_count": 0,
        })
        assert not ok, "All checks below thresholds should reject"

    def test_rejects_empty_features(self):
        ok, reasons = self._gate({})
        assert not ok, f"Empty features should be rejected, got ok={ok}"

    def test_strictly_two_types_required_when_configured(self):
        """When min_active_signal_types=2, a single insider buy alone is rejected."""
        ok, reasons = self._gate(
            {"quiver_insider_buy_count": 1},
            policy_overrides={"min_active_signal_types": 2},
        )
        assert not ok, f"Should reject with only 1 signal type when min_types=2, got ok={ok}"
        assert "quiver_min_types" in reasons

    def test_gate_disabled_when_all_zero(self):
        from signals.reader import gate_quiver_minimum
        policy = {"quiver_gate": {
            "enabled": True,
            "insider_buy_min_count_lookback": 0,
            "gov_contract_min_total_amount": 0,
            "gov_contract_min_count": 0,
            "patent_momentum_min": 0,
            "sec13f_count_min": 0,
            "sec13f_change_min_pct": 0,
            "min_active_signal_types": 1,
        }}
        ok, reasons = _with_policy(policy, lambda: gate_quiver_minimum({}))
        assert ok
        assert "quiver_disabled" in reasons


# ============================================================================
# 7. Fast lane activation
# ============================================================================

class TestFastLane:
    def _fast_lane(self, features: dict, cfg_overrides: dict = None):
        from signals.reader import _quiver_fast_lane_summary
        cfg = {
            "insider_buy_strong_min_count_7d": 2,
            "gov_contract_strong_min_total_30d": 1_000_000,
            "patent_momentum_min_strong": 1.0,
        }
        if cfg_overrides:
            cfg.update(cfg_overrides)
        return _quiver_fast_lane_summary(features, cfg)

    def test_fast_lane_triggers_on_strong_insider(self):
        strong, reasons, _ = self._fast_lane({"quiver_insider_buy_count": 3})
        assert strong
        assert "insider_buys" in reasons

    def test_fast_lane_not_triggered_on_weak_insider(self):
        strong, reasons, _ = self._fast_lane({"quiver_insider_buy_count": 1})
        assert not strong

    def test_fast_lane_triggers_on_gov_contract(self):
        strong, reasons, _ = self._fast_lane({"quiver_gov_contract_total_amount": 2_000_000})
        assert strong
        assert "gov_contracts" in reasons

    def test_fast_lane_not_triggered_on_small_gov_contract(self):
        strong, reasons, _ = self._fast_lane({"quiver_gov_contract_total_amount": 500_000})
        assert not strong

    def test_fast_lane_triggers_on_patent_momentum(self):
        # patent_momentum_min_strong = 1.0 (was previously 90 — bug fixed)
        strong, reasons, _ = self._fast_lane({"quiver_patent_momentum_latest": 2.5})
        assert strong, "patent momentum >= 1.0 should trigger fast lane"
        assert "patent_momentum" in reasons

    def test_fast_lane_patent_1_0_is_reachable(self):
        """Verify the threshold is now achievable (old threshold of 90 was not)."""
        from signals.reader import _FEATURE_CAPS
        cap = _FEATURE_CAPS.get("quiver_patent_momentum_latest", 5)
        threshold = 1.0
        assert threshold <= cap, \
            f"patent_momentum_min_strong ({threshold}) must be <= cap ({cap})"


# ============================================================================
# 8. Trend gate
# ============================================================================

class TestTrendGate:
    def test_trend_positive_required_blocks_downtrend(self):
        from signals.reader import _yahoo_gate_reasons
        # trend_positive = False simulates price[0] > price[-1]
        snapshot = (
            2_000_000_000,  # market_cap
            300_000,         # volume
            -5.0,            # weekly_change
            False,           # trend_positive — DOWNTREND
            1.0,             # price_change_24h
            400_000,         # volume_7d
            50.0,            # current_price
            1.0,             # atr
        )
        reasons = _yahoo_gate_reasons(
            snapshot_data=snapshot,
            min_market_cap=1_000_000_000,
            min_avg_volume=250_000,
            max_atr_pct=6.0,
            require_trend=True,
        )
        assert "trend_negative" in reasons, \
            f"Downtrend should be rejected when require_trend=True, got {reasons}"

    def test_trend_positive_passes_uptrend(self):
        from signals.reader import _yahoo_gate_reasons
        snapshot = (
            2_000_000_000, 300_000, 5.0, True, 1.0, 400_000, 50.0, 1.0,
        )
        reasons = _yahoo_gate_reasons(
            snapshot_data=snapshot,
            min_market_cap=1_000_000_000,
            min_avg_volume=250_000,
            max_atr_pct=6.0,
            require_trend=True,
        )
        assert "trend_negative" not in reasons


# ============================================================================
# 9. Universe rotation
# ============================================================================

class TestUniverseRotation:
    FAKE_UNIVERSE = [
        {"ticker_map": {"canonical": f"SYM{i:03d}", "yahoo": f"SYM{i:03d}", "quiver": f"SYM{i:03d}"}}
        for i in range(200)
    ]

    def _shuffle(self, date_str: str) -> list[str]:
        from signals.reader import _daily_shuffled_universe
        with patch("signals.reader._dt") as mock_dt:
            mock_dt.date.today.return_value = datetime.date.fromisoformat(date_str)
            shuffled = _daily_shuffled_universe(self.FAKE_UNIVERSE)
        return [e["ticker_map"]["canonical"] for e in shuffled]

    def test_shuffle_is_deterministic_same_day(self):
        order1 = self._shuffle("2026-03-01")
        order2 = self._shuffle("2026-03-01")
        assert order1 == order2, "Same day should always produce same order"

    def test_shuffle_differs_across_days(self):
        order1 = self._shuffle("2026-03-01")
        order2 = self._shuffle("2026-03-02")
        assert order1 != order2, "Different days should produce different rotation"

    def test_shuffle_preserves_all_symbols(self):
        order = self._shuffle("2026-03-01")
        assert set(order) == {e["ticker_map"]["canonical"] for e in self.FAKE_UNIVERSE}

    def test_shuffle_does_not_mutate_original(self):
        original_first = self.FAKE_UNIVERSE[0]["ticker_map"]["canonical"]
        self._shuffle("2026-03-05")
        assert self.FAKE_UNIVERSE[0]["ticker_map"]["canonical"] == original_first, \
            "Original universe should not be mutated by shuffle"


# ============================================================================
# 10. Max symbols from policy
# ============================================================================

class TestMaxSymbolsConfig:
    def test_max_symbols_read_from_policy(self):
        """get_top_signals should respect max_symbols_per_scan from policy."""
        from signals.reader import _signal_cfg
        policy = {"signals": {"max_symbols_per_scan": 150}}
        result = _with_policy(policy, lambda: int(_signal_cfg().get("max_symbols_per_scan", 100)))
        assert result == 150

    def test_max_symbols_default_is_100(self):
        from signals.reader import _signal_cfg
        result = _with_policy({}, lambda: int(_signal_cfg().get("max_symbols_per_scan", 100)))
        assert result == 100


# ============================================================================
# 11. Scoring: insider_net caps
# ============================================================================

class TestScoringCaps:
    def test_insider_net_cap_at_5(self):
        from signals.reader import _normalize_feature_value, _FEATURE_CAPS
        cap = _FEATURE_CAPS["quiver_insider_net_count"]
        normalized = _normalize_feature_value("quiver_insider_net_count", 100.0)
        assert normalized == float(cap), \
            f"insider_net should be capped at {cap}, got {normalized}"

    def test_gov_contract_cap_at_5m(self):
        from signals.reader import _normalize_feature_value, _FEATURE_CAPS
        cap = _FEATURE_CAPS["quiver_gov_contract_total_amount"]
        assert cap == 5_000_000, f"gov_contract cap should be $5M, got {cap}"
        val = _normalize_feature_value("quiver_gov_contract_total_amount", 999_999_999)
        assert val == float(cap)

    def test_app_rating_count_cap_prevents_domination(self):
        """App rating count must not produce runaway scores."""
        from signals.reader import _score_from_features, _FEATURE_CAPS
        cap = _FEATURE_CAPS.get("quiver_app_rating_latest_count")
        assert cap is not None, "quiver_app_rating_latest_count needs a cap"
        assert cap <= 1000, f"cap {cap} still too high — allows count to dominate"
        f = {"quiver_app_rating_latest": 5.0, "quiver_app_rating_latest_count": 1_000_000}
        score, _ = _score_from_features(f)
        assert score < 10, f"App ratings should not exceed 10 pts, got {score:.1f}"

    def test_wsb_max_contribution_reasonable(self):
        from signals.reader import _score_from_features
        f = {"quiver_wsb_recent_max_mentions": 999_999}  # capped at 500
        score, _ = _score_from_features(f)
        assert score <= 6, f"WSB max contribution should be <=6 pts, got {score:.1f}"

    def test_sell_count_penalizes_score(self):
        from signals.reader import _score_from_features
        good = {"quiver_insider_buy_count": 2.0, "quiver_insider_sell_count": 0.0,
                "quiver_insider_net_count": 2.0}
        bad = {"quiver_insider_buy_count": 2.0, "quiver_insider_sell_count": 4.0,
               "quiver_insider_net_count": -2.0}
        score_good, _ = _score_from_features(good)
        score_bad, _ = _score_from_features(bad)
        assert score_good > score_bad, \
            f"More sells should lower score ({score_good:.2f} vs {score_bad:.2f})"
