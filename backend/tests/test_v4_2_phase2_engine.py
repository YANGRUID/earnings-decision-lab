"""V4.2 Phase 2 -- the independent engine, end to end on a fake chain.

What these prove, in order of how badly each would hurt if it broke:

  1. the universe really spans several expiries, and each expiry carries its
     OWN implied move rather than the nearest one's copied forward;
  2. six configurations cost ONE market-data acquisition, not six;
  3. a structure priced above the control's $2,000 standardized capital stays
     in the universe and reaches the configurations that can hold it;
  4. nothing is written.

The provider is the same fake the multi-expiry construction tests use, so the
chain surface here is the one those tests already characterise.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from test_v4_2_multi_expiry_construction import FakeChainProvider

from analytics.decision.v4_2_phase2_methodology import PHASE_2_METHODOLOGY
from services.v4_2_phase2 import (
    PHASE2_STATUS_ACTION,
    PHASE2_STATUS_FAILED,
    PHASE2_STATUS_NO_ACTION,
    run_independent_search,
    summarize_phase2,
)

AS_OF = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)
EARNINGS = date(2026, 9, 10)
SETTLEMENT = date(2026, 9, 11)
LADDER = [date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25), date(2026, 10, 16)]


class _StubControlDecision:
    """Only the four fields Phase 2 reads off the control's frozen row. A
    real V4ShadowDecision would drag a whole event graph into a test about
    candidate search."""

    def __init__(self, ticker="MEXP", direction="neutral", volatility=None):
        self.ticker = ticker
        self.generated_at = AS_OF
        self.view_direction = direction
        self.view_volatility = volatility


def _run(db_session, provider, **kwargs):
    return run_independent_search(
        db_session,
        provider=provider,
        decision=_StubControlDecision(),
        settlement_date=SETTLEMENT,
        earnings_date=EARNINGS,
        as_of=AS_OF,
        **kwargs,
    )


class TestTheUniverseIsGenuinelyBroader:
    def test_the_search_spans_several_expiries(self, db_session):
        provider = FakeChainProvider(LADDER)

        result = _run(db_session, provider)

        assert result.status in (PHASE2_STATUS_ACTION, PHASE2_STATUS_NO_ACTION)
        assert result.expiries_considered > 1, "Phase 2 searched a single expiry"
        expiries = {d["expiration"] for d in result.candidate_detail.values()}
        assert len(expiries) > 1
        assert len(result.universe) > 0

    def test_each_expiry_carries_its_own_implied_move(self, db_session):
        """The fake chain prices a different straddle per expiry, so equal
        implied moves across rungs would mean one expiry's economics were
        cloned into the others."""
        provider = FakeChainProvider(LADDER)

        result = _run(db_session, provider)

        implied = [
            s.implied_move_pct
            for s in result.multi_expiry.per_expiry
            if s.implied_move_pct is not None
        ]
        assert len(implied) > 1
        assert len(set(implied)) == len(implied), f"implied move cloned across expiries: {implied}"

    def test_candidate_ids_carry_their_expiry(self, db_session):
        """The same geometry on two expiries is two instruments and must not
        collide in the universe."""
        provider = FakeChainProvider(LADDER)

        result = _run(db_session, provider)

        assert len({c.candidate_id for c in result.universe}) == len(result.universe)
        assert any("@" in c.candidate_id for c in result.universe)


class TestSixConfigurationsDoNotMultiplyMarketData:
    """Section 44."""

    def test_one_acquisition_serves_all_six(self, db_session):
        provider = FakeChainProvider(LADDER)

        result = _run(db_session, provider)

        assert len(result.configurations) == 6
        assert provider.underlying_calls == 1, "the underlying was quoted more than once"
        assert provider.metadata_calls == 1, "chain metadata was requested more than once"
        # One chain discovery and one selected-leg quote call per LADDER RUNG
        # -- never per configuration.
        assert len(provider.chain_calls) <= 3
        assert len(provider.quote_calls) <= 3
        assert len(provider.quote_calls) == len(set(provider.quote_calls))

    def test_contracts_are_deduplicated_before_quoting(self, db_session):
        provider = FakeChainProvider(LADDER)

        result = _run(db_session, provider)

        assert result.telemetry.contracts_deduplicated > 0
        assert result.telemetry.unique_contracts > 0
        assert result.telemetry.unique_contracts < sum(
            len(d["legs"]) for d in result.candidate_detail.values()
        )

    def test_telemetry_reports_every_stage(self, db_session):
        provider = FakeChainProvider(LADDER)

        result = _run(db_session, provider)

        stages = {s.name for s in result.telemetry.stages}
        assert stages == {"underlying", "metadata", "chain_discovery", "quotes"}
        assert result.telemetry.total_requests >= 4
        assert result.telemetry.total_latency_ms > 0


class ExpensiveChainProvider(FakeChainProvider):
    """A high-priced underlying, so single contracts land in the band between
    the $2,000 standardized capital and the $10,000 configurations -- exactly
    the band the control's screen deletes."""

    def __init__(self, expirations):
        super().__init__(expirations, strikes=[Decimal(str(s)) for s in range(400, 601, 5)])

    def get_underlying_quote(self, ticker):
        return super().get_underlying_quote(ticker).model_copy(update={"price": Decimal("500")})

    def _quote(self, ticker, expiration, strike, right):
        quote = super()._quote(ticker, expiration, strike, right)
        # ATM ~ $25/share, so an ATM long structure costs $2,500-$5,000 a
        # contract: above every $2K configuration's capital base, and inside
        # the $10K configurations' reach.
        distance = abs(strike - Decimal("500"))
        mid = max(Decimal("25") - distance / Decimal("10"), Decimal("0.05"))
        return quote.model_copy(
            update={
                "bid": mid - Decimal("0.05"),
                "ask": mid + Decimal("0.05"),
                "last_price": mid,
            }
        )


class TestTheControlCapitalScreenIsNotInherited:
    """The measured Phase-1 defect: V4.1 deletes anything costing more than
    the $2,000 standardized capital, and three configurations hold $10,000."""

    def test_a_structure_above_2000_stays_in_the_universe(self, db_session):
        result = _run(db_session, ExpensiveChainProvider(LADDER))

        expensive = [
            c
            for c in result.universe
            if c.entry_cash_required is not None and c.entry_cash_required > Decimal("2000")
        ]
        assert expensive, "the expensive fixture produced nothing above $2,000"
        assert all(c.data_valid for c in expensive), (
            "a candidate above the control's standardized capital was deleted from the "
            "shared universe instead of being left to each configuration"
        )

    def test_the_expensive_band_reaches_the_10k_configurations_only(self, db_session):
        result = _run(db_session, ExpensiveChainProvider(LADDER))
        by_key = {c.configuration.key: c for c in result.configurations}

        expensive_ids = {
            c.candidate_id
            for c in result.universe
            if c.entry_cash_required is not None and c.entry_cash_required > Decimal("2000")
        }
        assert expensive_ids

        # No $2K configuration can hold any of them.
        for key in ("v4_2k_conservative", "v4_2k_moderate", "v4_2k_aggressive"):
            rejected = {r.candidate_id for r in by_key[key].rejections}
            assert expensive_ids <= rejected, f"{key} ranked a structure it cannot afford"
            assert not (expensive_ids & set(by_key[key].ranked_candidate_ids))

        # For the two that permit the family, the refusal is named CAPITAL --
        # Conservative refuses a single-leg long at the family stage first,
        # which is the earlier and more specific answer.
        for key in ("v4_2k_moderate", "v4_2k_aggressive"):
            on_capital = {
                r.candidate_id for r in by_key[key].rejections if r.stage == "CAPITAL_INCOMPATIBLE"
            }
            assert expensive_ids & on_capital, f"{key} did not name capital as the constraint"

        # And at least one $10K configuration was able to consider one.
        considered = any(
            by_key[key].diagnostics.rankable_count > 0
            or {r.candidate_id for r in by_key[key].rejections if r.stage != "CAPITAL_INCOMPATIBLE"}
            & expensive_ids
            for key in ("v4_10k_conservative", "v4_10k_moderate", "v4_10k_aggressive")
        )
        assert considered, "no $10K configuration ever saw the expensive band"

    def test_capital_incompatible_is_never_a_universe_level_exclusion(self, db_session):
        result = _run(db_session, ExpensiveChainProvider(LADDER))

        invalid = [c.data_invalid_reason for c in result.universe if not c.data_valid]
        assert not any(
            "standardized per-decision capital" in (reason or "") for reason in invalid
        )


class TestFailureIsRecordedNotRaised:
    def test_a_provider_fault_becomes_phase_2_evidence(self, db_session):
        class Broken:
            def get_underlying_quote(self, ticker):
                raise RuntimeError("TWS is not connected")

        result = _run(db_session, Broken())

        assert result.status == PHASE2_STATUS_FAILED
        assert result.failure_category == "MARKET_DATA_UNAVAILABLE"
        assert "TWS is not connected" in (result.failure_detail or "")

    def test_no_listed_expiration_is_a_named_refusal(self, db_session):
        provider = FakeChainProvider([])

        result = _run(db_session, provider)

        assert result.status == PHASE2_STATUS_FAILED
        assert result.failure_category == "CHAIN_METADATA_FAILED"


class TestTheSummaryIsOperatorReadable:
    def test_all_six_rows_are_reported_even_when_they_agree(self, db_session):
        provider = FakeChainProvider(LADDER)

        summary = summarize_phase2(_run(db_session, provider))

        assert summary["methodology_version"] == PHASE_2_METHODOLOGY
        assert len(summary["configurations"]) == 6
        keys = [c["configuration_key"] for c in summary["configurations"]]
        assert keys == [
            "v4_2k_conservative",
            "v4_2k_moderate",
            "v4_2k_aggressive",
            "v4_10k_conservative",
            "v4_10k_moderate",
            "v4_10k_aggressive",
        ]
        for row in summary["configurations"]:
            assert row["status"] in ("ACTION", "NO_ACTION")
            assert row["reason"], f"{row['configuration_key']} reported no reason"
            assert row["diagnostics"]["universe_count"] == summary["candidate_universe"]

    def test_the_versions_block_names_every_sub_methodology(self, db_session):
        provider = FakeChainProvider(LADDER)

        summary = summarize_phase2(_run(db_session, provider))

        assert set(summary["versions"]) == {
            "methodology_version",
            "candidate_universe_version",
            "expiry_ladder_version",
            "ranking_version",
            "configuration_version",
            "move_edge_version",
            "viability_gate_version",
            "friction_version",
            "strategy_registry_version",
            "timing_policy_version",
        }


class TestZeroWrite:
    def test_running_the_engine_writes_nothing(self, db_session):
        from models.v4_2_challenger import V42ChallengerDecision  # noqa: PLC0415
        from models.v4_shadow import V4ShadowDecision  # noqa: PLC0415

        before = (
            db_session.query(V42ChallengerDecision).count(),
            db_session.query(V4ShadowDecision).count(),
        )

        _run(db_session, FakeChainProvider(LADDER))

        after = (
            db_session.query(V42ChallengerDecision).count(),
            db_session.query(V4ShadowDecision).count(),
        )
        assert before == after
        assert not db_session.new and not db_session.dirty and not db_session.deleted
