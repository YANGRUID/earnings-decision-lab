"""Phase 4 market-data-quality hardening (2026-08-26), Sections 16-17."""

from analytics.market_data_policy import (
    derive_capture_quality_label,
    enforce_market_data_quality_policy,
)
from models.enums import MarketDataQualityPolicy


class TestEnforceMarketDataQualityPolicy:
    def test_allow_delayed_with_label_accepts_everything(self):
        for values in (
            ["live"],
            ["delayed"],
            ["frozen", "delayed"],
            ["unavailable"],
            [None],
            [],
        ):
            assert (
                enforce_market_data_quality_policy(
                    MarketDataQualityPolicy.ALLOW_DELAYED_WITH_LABEL, values
                )
                is None
            )

    def test_live_only_accepts_all_live(self):
        assert (
            enforce_market_data_quality_policy(MarketDataQualityPolicy.LIVE_ONLY, ["live", "live"])
            is None
        )

    def test_live_only_rejects_any_delayed_leg(self):
        reason = enforce_market_data_quality_policy(
            MarketDataQualityPolicy.LIVE_ONLY, ["live", "delayed"]
        )
        assert reason is not None
        assert "delayed" in reason
        assert "live_only" in reason

    def test_live_only_rejects_unknown_quality(self):
        reason = enforce_market_data_quality_policy(MarketDataQualityPolicy.LIVE_ONLY, [None])
        assert reason is not None

    def test_live_only_rejects_frozen(self):
        reason = enforce_market_data_quality_policy(MarketDataQualityPolicy.LIVE_ONLY, ["frozen"])
        assert reason is not None
        assert "frozen" in reason


class TestDeriveCaptureQualityLabel:
    def test_all_live_is_verified_live(self):
        assert derive_capture_quality_label(["live", "live"]) == "VERIFIED_LIVE"

    def test_any_delayed_is_delayed_data(self):
        assert derive_capture_quality_label(["live", "delayed"]) == "DELAYED_DATA"

    def test_any_frozen_is_delayed_data(self):
        assert derive_capture_quality_label(["frozen"]) == "DELAYED_DATA"

    def test_any_unavailable_is_delayed_data(self):
        assert derive_capture_quality_label(["unavailable"]) == "DELAYED_DATA"

    def test_missing_value_is_unknown_quality(self):
        assert derive_capture_quality_label(["live", None]) == "UNKNOWN_QUALITY"

    def test_empty_is_unknown_quality(self):
        assert derive_capture_quality_label([]) == "UNKNOWN_QUALITY"

    def test_genuinely_unrecognized_value_is_unknown_quality(self):
        assert derive_capture_quality_label(["some_future_provider_flag"]) == "UNKNOWN_QUALITY"

    def test_never_labels_delayed_as_verified_live(self):
        """The explicit Section 16/17 requirement: Operations and Track
        Record must never label a delayed quote as live."""
        for values in (["delayed"], ["live", "delayed"], ["frozen", "live"]):
            assert derive_capture_quality_label(values) != "VERIFIED_LIVE"
