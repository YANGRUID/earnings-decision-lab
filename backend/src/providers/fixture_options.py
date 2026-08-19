"""A deterministic OptionsDataProvider backed entirely by real
OptionsSnapshot rows already in this database -- never a live network
call. Exists for exactly one purpose: giving the Expiration Selection
Engine (analytics/options/expiration_selection.py, services/
expiration_engine.py) and the Strategy Lab/AI Decision manual-expiration
override real, provider-shaped data to compare in the Playwright E2E
suite (frontend/e2e/) without depending on IBKR being reachable in CI
-- see scripts/seed_e2e_fixtures.py, which is this provider's only real
data source.

Selected via OPTIONS_PROVIDER=fixture (see providers/factory.py). Never
the default, and never wired into any production/dev deployment path --
an unconfigured deployment is entirely unaffected by this file existing.
"""

from datetime import date, datetime

from sqlalchemy.orm import Session

from models.options_snapshot import OptionsSnapshot
from providers.base import OptionsDataProvider
from providers.types import OptionQuote


class FixtureOptionsProvider(OptionsDataProvider):
    def __init__(self, db: Session) -> None:
        self._db = db

    def _rows(self, ticker: str, expiration: date | None) -> list[OptionsSnapshot]:
        from models.company import Company

        company = self._db.query(Company).filter(Company.ticker == ticker).one_or_none()
        if company is None:
            return []
        query = self._db.query(OptionsSnapshot).filter(OptionsSnapshot.company_id == company.id)
        if expiration is not None:
            query = query.filter(OptionsSnapshot.expiration_date == expiration)
        return query.order_by(OptionsSnapshot.expiration_date, OptionsSnapshot.strike).all()

    def get_option_chain(
        self,
        ticker: str,
        as_of: datetime,
        expiration: date | None = None,
        reference_date: date | None = None,
        earnings_anchored: bool = True,
    ) -> list[OptionQuote]:
        return [self._to_quote(row, ticker) for row in self._rows(ticker, expiration)]

    def list_available_expirations(
        self, ticker: str, after: date, max_candidates: int = 5
    ) -> list[date]:
        expirations = sorted({row.expiration_date for row in self._rows(ticker, None)})
        return [e for e in expirations if e > after][:max_candidates]

    @staticmethod
    def _to_quote(row: OptionsSnapshot, ticker: str) -> OptionQuote:
        return OptionQuote(
            ticker=ticker,
            snapshot_timestamp=row.snapshot_timestamp,
            expiration_date=row.expiration_date,
            strike=row.strike,
            option_type=row.option_type.value,
            bid=row.bid,
            ask=row.ask,
            last_price=row.last_price,
            volume=row.volume,
            open_interest=row.open_interest,
            implied_volatility=row.implied_volatility,
            delta=row.delta,
            gamma=row.gamma,
            theta=row.theta,
            vega=row.vega,
            market_data_quality=row.market_data_quality.value if row.market_data_quality else None,
            source_provider=row.source_provider,
            retrieved_at=row.retrieved_at,
        )
