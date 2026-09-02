from datetime import UTC, date, datetime
from decimal import Decimal

from agents.tools.guidance_comparison import GuidanceComparisonArgs, GuidanceComparisonTool
from models.ai_extraction import AIExtraction
from models.company import Company
from models.enums import FilingType
from models.filing import Filing
from schemas.extraction import GuidanceExtraction, RevenueGuidance

NOW = datetime.now(UTC)


def _make_filing(db_session, company, filing_date, accession) -> Filing:
    filing = Filing(
        company_id=company.id,
        filing_type=FilingType.FORM_10Q,
        filing_date=filing_date,
        accession_number=accession,
        source_url=f"https://example.com/{accession}.htm",
        retrieved_at=NOW,
    )
    db_session.add(filing)
    db_session.flush()
    return filing


def _make_extraction(db_session, company, filing, extraction: GuidanceExtraction) -> AIExtraction:
    row = AIExtraction(
        filing_id=filing.id,
        company_id=company.id,
        extraction_type="guidance",
        extracted_data=extraction.model_dump(mode="json"),
        source_chunk_ids=[],
        model="test-model",
        prompt_version="test-v1",
        retrieved_at=NOW,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_compares_two_most_recent_extractions(db_session):
    company = Company(ticker="ZZAGT4", name="ZZ Agent Test 4", cik="0009990004")
    db_session.add(company)
    db_session.flush()

    older_filing = _make_filing(db_session, company, date(2025, 12, 18), "TEST-AGT-0010")
    newer_filing = _make_filing(db_session, company, date(2026, 3, 19), "TEST-AGT-0011")
    _make_extraction(
        db_session,
        company,
        older_filing,
        GuidanceExtraction(revenue=RevenueGuidance(low=Decimal("100"), high=Decimal("120"))),
    )
    _make_extraction(
        db_session,
        company,
        newer_filing,
        GuidanceExtraction(revenue=RevenueGuidance(low=Decimal("110"), high=Decimal("130"))),
    )

    tool = GuidanceComparisonTool(db_session)
    outcome = tool.run(GuidanceComparisonArgs(ticker="ZZAGT4"))

    assert outcome.success
    assert outcome.data["revenue"]["previous_midpoint"] == "110"
    assert outcome.data["revenue"]["current_midpoint"] == "120"
    assert outcome.data["revenue"]["midpoint_change"] == "10"


def test_insufficient_extractions_returns_honest_message(db_session):
    company = Company(ticker="ZZAGT5", name="ZZ Agent Test 5", cik="0009990005")
    db_session.add(company)
    db_session.flush()
    filing = _make_filing(db_session, company, date(2026, 3, 19), "TEST-AGT-0012")
    _make_extraction(db_session, company, filing, GuidanceExtraction())

    tool = GuidanceComparisonTool(db_session)
    outcome = tool.run(GuidanceComparisonArgs(ticker="ZZAGT5"))

    assert outcome.success
    assert outcome.data["available_extractions"] == 1
    assert "Only 1 guidance extraction" in outcome.summary


def test_unknown_ticker_returns_empty(db_session):
    tool = GuidanceComparisonTool(db_session)
    outcome = tool.run(GuidanceComparisonArgs(ticker="NOSUCHTICKER"))

    assert outcome.success
    assert outcome.data == {}


class TestPointInTimeCutoff:
    """Phase 4 point-in-time hardening (2026-08-26), Section 20 -- a
    historical/replay caller must never see guidance filed after its
    real cutoff, exactly like earnings_history.py/filings_search.py's
    own as_of already enforce."""

    def test_filing_after_cutoff_excluded(self, db_session):
        company = Company(ticker="ZZAGT6", name="ZZ Agent Test 6", cik="0009990006")
        db_session.add(company)
        db_session.flush()

        before_cutoff = _make_filing(db_session, company, date(2025, 12, 18), "TEST-AGT-0013")
        after_cutoff = _make_filing(db_session, company, date(2026, 3, 19), "TEST-AGT-0014")
        _make_extraction(
            db_session,
            company,
            before_cutoff,
            GuidanceExtraction(revenue=RevenueGuidance(low=Decimal("100"), high=Decimal("120"))),
        )
        _make_extraction(
            db_session,
            company,
            after_cutoff,
            GuidanceExtraction(revenue=RevenueGuidance(low=Decimal("999"), high=Decimal("999"))),
        )

        tool = GuidanceComparisonTool(db_session)
        outcome = tool.run(GuidanceComparisonArgs(ticker="ZZAGT6", as_of=date(2026, 1, 1)))

        # Only one real extraction exists on or before the cutoff -- never
        # enough to compare, and the future one's numbers must never leak
        # into the honest "not enough data" message either.
        assert outcome.success
        assert outcome.data["available_extractions"] == 1
        assert "999" not in str(outcome.data)

    def test_no_as_of_is_unrestricted_current_behavior(self, db_session):
        """None (the default, every real caller today) means "as of
        now" -- unchanged behavior for the live research chat."""
        company = Company(ticker="ZZAGT7", name="ZZ Agent Test 7", cik="0009990007")
        db_session.add(company)
        db_session.flush()
        older = _make_filing(db_session, company, date(2025, 12, 18), "TEST-AGT-0015")
        newer = _make_filing(db_session, company, date(2026, 3, 19), "TEST-AGT-0016")
        _make_extraction(
            db_session,
            company,
            older,
            GuidanceExtraction(revenue=RevenueGuidance(low=Decimal("100"), high=Decimal("120"))),
        )
        _make_extraction(
            db_session,
            company,
            newer,
            GuidanceExtraction(revenue=RevenueGuidance(low=Decimal("110"), high=Decimal("130"))),
        )

        tool = GuidanceComparisonTool(db_session)
        outcome = tool.run(GuidanceComparisonArgs(ticker="ZZAGT7"))

        assert outcome.success
        assert outcome.data["revenue"]["current_midpoint"] == "120"
