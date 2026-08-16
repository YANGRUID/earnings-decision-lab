from datetime import date, datetime
from decimal import Decimal

from providers.base import EarningsDataProvider, OptionsDataProvider, TranscriptProvider
from providers.fixtures import (
    FixtureEarningsDataProvider,
    FixtureOptionsDataProvider,
    FixtureTranscriptProvider,
)


def test_fixture_earnings_provider_conforms_to_interface():
    provider = FixtureEarningsDataProvider()
    assert isinstance(provider, EarningsDataProvider)

    calendar = provider.get_earnings_calendar("MU")
    assert len(calendar) == 1
    assert calendar[0].earnings_date == date(2025, 9, 23)

    estimate = provider.get_consensus_estimate("MU", 2025, 4, datetime(2025, 9, 15))
    assert estimate is not None
    assert estimate.consensus_eps == Decimal("2.85")

    assert provider.get_consensus_estimate("ZZZZ", 2025, 4, datetime(2025, 9, 15)) is None


def test_fixture_options_provider_conforms_to_interface():
    provider = FixtureOptionsDataProvider()
    assert isinstance(provider, OptionsDataProvider)

    quotes = provider.get_option_chain("MU", datetime(2025, 9, 22))
    assert len(quotes) == 2
    assert {q.option_type for q in quotes} == {"call", "put"}


def test_fixture_transcript_provider_conforms_to_interface():
    provider = FixtureTranscriptProvider()
    assert isinstance(provider, TranscriptProvider)

    transcript = provider.get_transcript("MU", 2025, 4)
    assert transcript is not None
    assert "FIXTURE TRANSCRIPT" in transcript.text

    assert provider.get_transcript("MU", 2020, 1) is None
