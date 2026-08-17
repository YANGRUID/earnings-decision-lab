from datetime import UTC, datetime
from decimal import Decimal

from models.portfolio_position_snapshot import PortfolioPositionSnapshot
from providers.types import PortfolioPosition
from services.portfolio import collect_portfolio_snapshot, get_latest_portfolio_snapshot

NOW = datetime.now(UTC)


class _StubPortfolioProvider:
    def __init__(self, positions: list[PortfolioPosition]) -> None:
        self._positions = positions

    def get_positions(self) -> list[PortfolioPosition]:
        return self._positions


def _position(**overrides) -> PortfolioPosition:
    defaults = dict(
        account_id_masked="U99****99",
        conid=672387468,
        contract_description="MNQ MAR2025",
        asset_class="FUT",
        quantity=Decimal("2"),
        currency="USD",
        market_price=Decimal("21770.43"),
        market_value=Decimal("87081.72"),
        average_cost=Decimal("43536.12"),
        unrealized_pnl=Decimal("9.48"),
        realized_pnl=Decimal("0"),
        source_provider="ibkr",
        retrieved_at=NOW,
    )
    defaults.update(overrides)
    return PortfolioPosition(**defaults)


class TestCollectPortfolioSnapshot:
    def test_persists_real_positions(self, db_session):
        provider = _StubPortfolioProvider([_position()])

        rows = collect_portfolio_snapshot(db_session, provider)

        assert len(rows) == 1
        assert rows[0].conid == 672387468
        assert rows[0].account_id_masked == "U99****99"
        persisted = db_session.query(PortfolioPositionSnapshot).count()
        assert persisted == 1

    def test_empty_account_persists_nothing_and_is_not_an_error(self, db_session):
        provider = _StubPortfolioProvider([])

        rows = collect_portfolio_snapshot(db_session, provider)

        assert rows == []
        assert db_session.query(PortfolioPositionSnapshot).count() == 0

    def test_two_collections_are_both_kept_as_separate_point_in_time_batches(self, db_session):
        provider = _StubPortfolioProvider([_position()])

        collect_portfolio_snapshot(db_session, provider)
        collect_portfolio_snapshot(db_session, provider)

        # Never overwritten -- both real snapshots exist, each with its own
        # snapshot_timestamp, even though the position itself didn't change.
        assert db_session.query(PortfolioPositionSnapshot).count() == 2


class TestGetLatestPortfolioSnapshot:
    def test_returns_empty_list_when_nothing_ever_collected(self, db_session):
        assert get_latest_portfolio_snapshot(db_session) == []

    def test_returns_only_the_most_recent_batch(self, db_session):
        older_ts = datetime(2026, 1, 1, tzinfo=UTC)
        newer_ts = datetime(2026, 6, 1, tzinfo=UTC)
        db_session.add(
            PortfolioPositionSnapshot(
                account_id_masked="U99****99",
                snapshot_timestamp=older_ts,
                conid=1,
                contract_description="OLD POSITION",
                asset_class="STK",
                quantity=Decimal("10"),
                source_provider="ibkr",
                retrieved_at=older_ts,
            )
        )
        db_session.add(
            PortfolioPositionSnapshot(
                account_id_masked="U99****99",
                snapshot_timestamp=newer_ts,
                conid=2,
                contract_description="NEW POSITION",
                asset_class="STK",
                quantity=Decimal("5"),
                source_provider="ibkr",
                retrieved_at=newer_ts,
            )
        )
        db_session.flush()

        latest = get_latest_portfolio_snapshot(db_session)

        assert len(latest) == 1
        assert latest[0].contract_description == "NEW POSITION"
