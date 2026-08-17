from decimal import Decimal

import pytest
from pydantic import ValidationError

from schemas.extraction import (
    EPSGuidance,
    GuidanceExtraction,
    ManagementTone,
    RevenueGuidance,
)


def test_range_guidance_computes_midpoint():
    g = RevenueGuidance(low=Decimal("100"), high=Decimal("120"))
    assert g.midpoint == Decimal("110")


def test_range_guidance_midpoint_with_only_low():
    g = RevenueGuidance(low=Decimal("100"))
    assert g.midpoint == Decimal("100")


def test_range_guidance_midpoint_none_when_both_missing():
    g = RevenueGuidance()
    assert g.midpoint is None


def test_range_guidance_rejects_low_greater_than_high():
    with pytest.raises(ValidationError):
        RevenueGuidance(low=Decimal("120"), high=Decimal("100"))


def test_eps_guidance_accepts_negative_values():
    # EPS guidance can legitimately be negative (a guided loss).
    g = EPSGuidance(low=Decimal("-0.50"), high=Decimal("-0.20"))
    assert g.midpoint == Decimal("-0.35")


def test_guidance_extraction_defaults_are_empty_not_none():
    extraction = GuidanceExtraction()
    assert extraction.revenue is None
    assert extraction.key_drivers == []
    assert extraction.risks == []
    assert extraction.important_topics == []


def test_guidance_extraction_full_round_trip():
    extraction = GuidanceExtraction(
        revenue=RevenueGuidance(low=Decimal("11000"), high=Decimal("11400"), period="Q1 FY2027"),
        management_tone=ManagementTone(overall="positive", summary="Strong demand commentary."),
        key_drivers=["HBM demand", "AI server growth"],
        risks=["DRAM pricing volatility"],
        important_topics=["HBM", "capex"],
    )
    dumped = extraction.model_dump(mode="json")
    restored = GuidanceExtraction.model_validate(dumped)

    assert restored.revenue.midpoint == Decimal("11200")
    assert restored.management_tone.overall == "positive"
    assert restored.key_drivers == ["HBM demand", "AI server growth"]


def test_management_tone_rejects_invalid_literal():
    with pytest.raises(ValidationError):
        ManagementTone(overall="ecstatic", summary="x")
