from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from models.company import Company
from models.document_chunk import DocumentChunk
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.filing import Filing
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.research_preparation_job import (
    JobStatus,
    PreparationStep,
    ResearchPreparationJob,
    StepStatus,
)
from providers.base import EarningsEstimatesProvider, MarketDataProvider, OptionsDataProvider
from providers.types import (
    CompanyFacts,
    CompanyFactValue,
    FilingMetadata,
    OHLCBar,
    OptionQuote,
    UpcomingEarningsCalendarEntry,
)
from rag.embeddings import EMBEDDING_DIM, EmbeddingProvider
from services.research_orchestration import (
    ResearchProviders,
    UnsupportedSymbolError,
    prepare_company_research,
)

SAMPLE_FILING_HTML = """
<html><body>
<p>Item 1. Business</p>
<p>We design and sell real products to real customers in a real market.</p>
</body></html>
"""

# Computed at import time (not a hardcoded literal) so it's always extremely
# close to whatever `datetime.now(UTC)` the reused ingestion helpers this
# module calls into (e.g. ingestion.bootstrap_phase1's `_upsert_earnings_
# event_and_result`) stamp real rows with internally -- those helpers aren't
# parameterized to accept an injected clock, so freshness-reuse assertions
# below stay deterministic regardless of which real calendar day the suite
# happens to run on.
NOW = datetime.now(UTC)


class _FakeEdgar:
    def __init__(
        self,
        cik: str = "0009999901",
        entity_name: str = "ZZ New Co",
        eps_values: list[CompanyFactValue] | None = None,
        filings: list[FilingMetadata] | None = None,
    ) -> None:
        self.cik = cik
        self.entity_name = entity_name
        self.eps_values = eps_values or []
        self.filings = filings if filings is not None else []
        self.facts_calls = 0
        self.search_calls = 0

    def lookup_cik(self, ticker: str) -> str | None:
        return self.cik

    def get_company_facts(self, cik: str) -> CompanyFacts:
        self.facts_calls += 1
        return CompanyFacts(
            cik=cik,
            entity_name=self.entity_name,
            eps_diluted=self.eps_values,
            revenues=[],
            source_provider="sec_edgar_xbrl",
            retrieved_at=datetime.now(UTC),
        )

    def search_filings(
        self, cik: str, filing_types: list[str], limit: int = 10
    ) -> list[FilingMetadata]:
        self.search_calls += 1
        return [f for f in self.filings if f.filing_type in filing_types][:limit]

    def get_filing_html(self, source_url: str) -> str:
        return SAMPLE_FILING_HTML


class _FakeMarketData(MarketDataProvider):
    def __init__(self, retrieved_at: datetime = NOW) -> None:
        self.tickers_fetched: list[str] = []
        self._retrieved_at = retrieved_at

    def get_daily_bars(self, ticker: str, start: date, end: date) -> list[OHLCBar]:
        self.tickers_fetched.append(ticker)
        base = date(2025, 1, 1)
        return [
            OHLCBar(
                ticker=ticker,
                trade_date=base + timedelta(days=i),
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10.5"),
                volume=1000,
                source_provider="fake",
                retrieved_at=self._retrieved_at,
            )
            for i in range(3)
        ]


class _FakeEstimatesProvider(EarningsEstimatesProvider):
    def __init__(self, next_report_date: date | None) -> None:
        self._next_report_date = next_report_date
        self.calls = 0

    def get_earnings_estimates(self, ticker: str) -> list:
        return []

    def get_next_earnings_date(self, ticker: str) -> UpcomingEarningsCalendarEntry | None:
        self.calls += 1
        if self._next_report_date is None:
            return None
        return UpcomingEarningsCalendarEntry(
            ticker=ticker,
            fiscal_period_end_date=self._next_report_date,
            estimated_report_date=self._next_report_date,
            source_provider="fake",
            retrieved_at=datetime.now(UTC),
        )


class _FakeOptionsProvider(OptionsDataProvider):
    def __init__(
        self, quotes: list[OptionQuote] | None = None, error: Exception | None = None
    ) -> None:
        self._quotes = quotes if quotes is not None else []
        self._error = error
        self.calls = 0

    def get_option_chain(self, ticker, as_of, expiration=None, reference_date=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._quotes


def _stub_option_quote(strike: Decimal, option_type: str, expiration: date) -> OptionQuote:
    now = datetime.now(UTC)
    return OptionQuote(
        ticker="ZZNEWC",
        snapshot_timestamp=now,
        expiration_date=expiration,
        strike=strike,
        option_type=option_type,
        bid=Decimal("1.00"),
        ask=Decimal("1.20"),
        source_provider="fake",
        retrieved_at=now,
    )


class _FakeEmbedder(EmbeddingProvider):
    model_name = "fake-model"
    dimension = EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * EMBEDDING_DIM for _ in texts]


def _providers(
    edgar: _FakeEdgar | None = None,
    market_data: _FakeMarketData | None = None,
    estimates: EarningsEstimatesProvider | None = None,
    options: OptionsDataProvider | None = None,
) -> ResearchProviders:
    return ResearchProviders(
        edgar=edgar or _FakeEdgar(),
        market_data=market_data or _FakeMarketData(),
        estimates=estimates,
        options=options,
        embedder=_FakeEmbedder(),
    )


def test_unsupported_symbol_raises_before_creating_any_job(db_session):
    try:
        prepare_company_research(db_session, "not a ticker!!!", _providers(), now=NOW)
        raise AssertionError("expected UnsupportedSymbolError")
    except UnsupportedSymbolError:
        pass
    assert db_session.query(ResearchPreparationJob).count() == 0


def test_new_ticker_creates_company_and_completes_required_steps(db_session):
    edgar = _FakeEdgar(
        cik="0009999902",
        entity_name="ZZ New Co",
        eps_values=[
            CompanyFactValue(
                fiscal_year=2025,
                fiscal_period="Q1",
                value=Decimal("1.23"),
                unit="USD/shares",
                filed_date=date(2025, 3, 1),
                end_date=date(2025, 1, 31),
                accession_number="0000000000-25-000001",
                form="10-Q",
            )
        ],
        filings=[
            FilingMetadata(
                cik="0009999902",
                company_name="ZZ New Co",
                filing_type="10-K",
                filing_date=date(2025, 3, 1),
                accession_number="0000000000-25-000002",
                primary_document="doc.htm",
                source_url="https://example.com/doc.htm",
                source_provider="sec_edgar",
                retrieved_at=datetime.now(UTC),
            )
        ],
    )
    providers = _providers(edgar=edgar, estimates=_FakeEstimatesProvider(None))

    job = prepare_company_research(db_session, "zznewc", providers, now=NOW)

    assert job.status == JobStatus.COMPLETED
    assert job.ticker == "ZZNEWC"
    company = db_session.query(Company).filter(Company.ticker == "ZZNEWC").one()
    assert job.company_id == company.id
    assert company.cik == "0009999902"
    assert company.sector is None  # honestly unknown, never guessed

    steps_by_name = {s["step"]: s for s in job.steps}
    assert (
        steps_by_name[PreparationStep.COMPANY_IDENTIFIED.value]["status"] == StepStatus.DONE.value
    )
    assert (
        steps_by_name[PreparationStep.HISTORICAL_EARNINGS.value]["status"] == StepStatus.DONE.value
    )
    assert steps_by_name[PreparationStep.PRICE_HISTORY.value]["status"] == StepStatus.DONE.value
    assert steps_by_name[PreparationStep.SEC_FILINGS.value]["status"] == StepStatus.DONE.value
    assert steps_by_name[PreparationStep.FILING_EMBEDDINGS.value]["status"] == StepStatus.DONE.value
    # No estimates provider entry -> no known upcoming date -> options chain skipped.
    assert (
        steps_by_name[PreparationStep.EARNINGS_ESTIMATES.value]["status"]
        == StepStatus.SKIPPED.value
    )
    assert steps_by_name[PreparationStep.OPTIONS_CHAIN.value]["status"] == StepStatus.SKIPPED.value

    assert db_session.query(PriceBar).filter(PriceBar.ticker == "ZZNEWC").count() > 0
    assert db_session.query(Filing).filter(Filing.company_id == company.id).count() == 1
    assert (
        db_session.query(DocumentChunk).filter(DocumentChunk.company_id == company.id).count() > 0
    )


def test_second_call_reuses_fresh_data_without_refetching(db_session):
    edgar = _FakeEdgar(
        cik="0009999903",
        entity_name="ZZ Fresh Co",
        eps_values=[
            CompanyFactValue(
                fiscal_year=2025,
                fiscal_period="Q1",
                value=Decimal("0.50"),
                unit="USD/shares",
                filed_date=date(2025, 3, 1),
                end_date=date(2025, 1, 31),
                accession_number="0000000000-25-000003",
                form="10-Q",
            )
        ],
    )
    market_data = _FakeMarketData()
    providers = _providers(edgar=edgar, market_data=market_data)

    prepare_company_research(db_session, "zzfrsh", providers, now=NOW)
    facts_calls_after_first = edgar.facts_calls
    tickers_after_first = list(market_data.tickers_fetched)

    job2 = prepare_company_research(db_session, "zzfrsh", providers, now=NOW + timedelta(minutes=5))

    assert job2.status == JobStatus.COMPLETED
    steps_by_name = {s["step"]: s for s in job2.steps}
    assert steps_by_name[PreparationStep.COMPANY_IDENTIFIED.value]["detail"] == "already on record"
    assert "already fresh" in steps_by_name[PreparationStep.HISTORICAL_EARNINGS.value]["detail"]
    assert "already fresh" in steps_by_name[PreparationStep.PRICE_HISTORY.value]["detail"]
    # No new SEC facts calls and no new bar fetches on the second, still-fresh run.
    assert edgar.facts_calls == facts_calls_after_first
    assert market_data.tickers_fetched == tickers_after_first


def test_force_refetches_even_when_fresh(db_session):
    edgar = _FakeEdgar(
        cik="0009999904",
        entity_name="ZZ Force Co",
        eps_values=[
            CompanyFactValue(
                fiscal_year=2025,
                fiscal_period="Q1",
                value=Decimal("0.75"),
                unit="USD/shares",
                filed_date=date(2025, 3, 1),
                end_date=date(2025, 1, 31),
                accession_number="0000000000-25-000004",
                form="10-Q",
            )
        ],
    )
    market_data = _FakeMarketData()
    providers = _providers(edgar=edgar, market_data=market_data)

    prepare_company_research(db_session, "zzfrce", providers, now=NOW)
    facts_calls_after_first = edgar.facts_calls
    bars_calls_after_first = len(market_data.tickers_fetched)

    job2 = prepare_company_research(
        db_session, "zzfrce", providers, now=NOW + timedelta(minutes=5), force=True
    )

    assert job2.status == JobStatus.COMPLETED
    steps_by_name = {s["step"]: s for s in job2.steps}
    assert "already fresh" not in steps_by_name[PreparationStep.HISTORICAL_EARNINGS.value]["detail"]
    assert "already fresh" not in steps_by_name[PreparationStep.PRICE_HISTORY.value]["detail"]
    assert edgar.facts_calls > facts_calls_after_first
    assert len(market_data.tickers_fetched) > bars_calls_after_first


def test_required_step_failure_fails_whole_job(db_session):
    class _BrokenMarketData(MarketDataProvider):
        def get_daily_bars(self, ticker, start, end):
            raise RuntimeError("market data provider is down")

    providers = _providers(market_data=_BrokenMarketData())

    job = prepare_company_research(db_session, "zzbrkn", providers, now=NOW)

    assert job.status == JobStatus.FAILED
    assert job.error is not None
    assert "market data provider is down" in job.error
    steps_by_name = {s["step"]: s for s in job.steps}
    assert steps_by_name[PreparationStep.PRICE_HISTORY.value]["status"] == StepStatus.FAILED.value
    # Steps after the failed required step never ran.
    assert steps_by_name[PreparationStep.SEC_FILINGS.value]["status"] == StepStatus.PENDING.value


def test_optional_provider_failure_completes_with_warnings(db_session):
    options = _FakeOptionsProvider(error=RuntimeError("options gateway unreachable"))
    providers = _providers(
        estimates=_FakeEstimatesProvider(date(2026, 9, 1)),
        options=options,
    )

    job = prepare_company_research(db_session, "zzwarn", providers, now=NOW)

    assert job.status == JobStatus.COMPLETED_WITH_WARNINGS
    steps_by_name = {s["step"]: s for s in job.steps}
    assert steps_by_name[PreparationStep.OPTIONS_CHAIN.value]["status"] == StepStatus.FAILED.value
    assert (
        "options gateway unreachable"
        in steps_by_name[PreparationStep.OPTIONS_CHAIN.value]["detail"]
    )
    # Required steps still ran to completion despite the later optional failure.
    assert steps_by_name[PreparationStep.PRICE_HISTORY.value]["status"] == StepStatus.DONE.value


def test_options_chain_fetched_when_upcoming_earnings_date_known(db_session):
    report_date = date(2026, 9, 1)
    expiration = date(2026, 9, 4)
    options = _FakeOptionsProvider(
        quotes=[
            _stub_option_quote(Decimal("10"), "call", expiration),
            _stub_option_quote(Decimal("10"), "put", expiration),
        ]
    )
    providers = _providers(estimates=_FakeEstimatesProvider(report_date), options=options)

    job = prepare_company_research(db_session, "zzoptc", providers, now=NOW)

    assert job.status == JobStatus.COMPLETED
    steps_by_name = {s["step"]: s for s in job.steps}
    assert steps_by_name[PreparationStep.OPTIONS_CHAIN.value]["status"] == StepStatus.DONE.value
    assert options.calls == 1
    company = db_session.query(Company).filter(Company.ticker == "ZZOPTC").one()
    assert (
        db_session.query(OptionsSnapshot).filter(OptionsSnapshot.company_id == company.id).count()
        == 2
    )


def test_existing_estimate_snapshot_reused_when_fresh(db_session):
    company = Company(ticker="ZZESTC", name="ZZ Estimate Co", cik="0009999905")
    db_session.add(company)
    db_session.flush()
    db_session.add(
        EarningsEstimateSnapshot(
            company_id=company.id,
            fiscal_period_end_date=date(2026, 6, 30),
            horizon="fiscal quarter",
            snapshot_timestamp=NOW - timedelta(hours=1),
            estimated_report_date=date(2026, 9, 1),
            source_provider="fake",
            retrieved_at=NOW - timedelta(hours=1),
        )
    )
    db_session.commit()

    estimates = _FakeEstimatesProvider(date(2026, 9, 1))
    providers = _providers(estimates=estimates)

    job = prepare_company_research(db_session, "zzestc", providers, now=NOW)

    assert job.status == JobStatus.COMPLETED
    assert estimates.calls == 0  # fresh (< 12h old) -- never re-fetched
    steps_by_name = {s["step"]: s for s in job.steps}
    assert "already fresh" in steps_by_name[PreparationStep.EARNINGS_ESTIMATES.value]["detail"]


def test_existing_estimate_snapshot_refetched_when_stale(db_session):
    # Distinct from the MISSING (never-collected) case exercised elsewhere:
    # this is data that exists but has aged past its freshness policy
    # window (EARNINGS_ESTIMATES = 12h), which must trigger a real
    # refetch just like MISSING does -- needs_refresh() treats both the
    # same way, but this exercises that via a genuinely stale row rather
    # than an absent one.
    company = Company(ticker="ZZSTAL", name="ZZ Stale Co", cik="0009999906")
    db_session.add(company)
    db_session.flush()
    stale_snapshot_time = NOW - timedelta(hours=13)
    db_session.add(
        EarningsEstimateSnapshot(
            company_id=company.id,
            fiscal_period_end_date=date(2026, 6, 30),
            horizon="fiscal quarter",
            snapshot_timestamp=stale_snapshot_time,
            estimated_report_date=date(2026, 9, 1),
            source_provider="fake",
            retrieved_at=stale_snapshot_time,
        )
    )
    db_session.commit()

    estimates = _FakeEstimatesProvider(date(2026, 9, 1))
    providers = _providers(estimates=estimates)

    job = prepare_company_research(db_session, "zzstal", providers, now=NOW)

    assert job.status == JobStatus.COMPLETED
    assert estimates.calls == 1  # stale -> real refetch, not skipped
    steps_by_name = {s["step"]: s for s in job.steps}
    assert "already fresh" not in steps_by_name[PreparationStep.EARNINGS_ESTIMATES.value]["detail"]
    # Point-in-time integrity: the stale row is never overwritten in
    # place -- the refetch adds a new snapshot alongside it.
    rows = (
        db_session.query(EarningsEstimateSnapshot)
        .filter(EarningsEstimateSnapshot.company_id == company.id)
        .all()
    )
    assert len(rows) == 2
    assert stale_snapshot_time in {r.snapshot_timestamp for r in rows}


def test_force_refresh_never_overwrites_prior_options_snapshots(db_session):
    # Point-in-time integrity for the options-chain step specifically:
    # a forced re-collection must add a new OptionsSnapshot row per quote,
    # never update an existing historical row in place.
    report_date = date(2026, 9, 1)
    expiration = date(2026, 9, 4)
    options = _FakeOptionsProvider(
        quotes=[
            _stub_option_quote(Decimal("10"), "call", expiration),
            _stub_option_quote(Decimal("10"), "put", expiration),
        ]
    )
    providers = _providers(estimates=_FakeEstimatesProvider(report_date), options=options)

    prepare_company_research(db_session, "zzpitc", providers, now=NOW)
    company = db_session.query(Company).filter(Company.ticker == "ZZPITC").one()
    first_count = (
        db_session.query(OptionsSnapshot).filter(OptionsSnapshot.company_id == company.id).count()
    )
    assert first_count == 2

    prepare_company_research(
        db_session, "zzpitc", providers, now=NOW + timedelta(days=1), force=True
    )
    second_count = (
        db_session.query(OptionsSnapshot).filter(OptionsSnapshot.company_id == company.id).count()
    )

    assert options.calls == 2  # a real second fetch happened
    assert second_count == 4  # new rows added, old ones untouched
    assert (
        db_session.query(OptionsSnapshot)
        .filter(OptionsSnapshot.company_id == company.id, OptionsSnapshot.strike == Decimal("10"))
        .count()
        == 4
    )
