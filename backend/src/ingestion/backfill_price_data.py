"""Backfill real daily price history and price-derived point-in-time
snapshots, using Tiingo (primary) with Alpha Vantage as fallback.

Run: uv run python -m ingestion.backfill_price_data

Requires TIINGO_API_KEY and/or ALPHA_VANTAGE_API_KEY in .env — run
ingestion.backfill_earnings_dates first so events have a confirmed
earnings_date to anchor snapshots to.

Scope (see docs/limitations.md):
  - earnings_expectation_snapshot gets exactly the fields computable from
    daily closes: stock_price (last close before earnings), recent_return_5d
    /20d, sector_return (SOXX), market_return (SPY). All IV/options/
    consensus fields stay null — no real source for them yet.
  - price_reaction gets real next-day/five-day close and move percentages.
    after_hours_price stays null — daily bars have no intraday data.
  - Only events with a confirmed earnings_date (see backfill_earnings_dates)
    get a snapshot; the rest have no anchor point to backfill against.
"""

import logging
from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.earnings.price_moves import (
    InsufficientPriceHistory,
    price_reaction_moves,
    recent_return,
)
from core.config import get_settings
from db.session import SessionLocal
from models.company import Company
from models.earnings_event import EarningsEvent
from models.earnings_expectation_snapshot import EarningsExpectationSnapshot
from models.price_bar import PriceBar
from models.price_reaction import PriceReaction
from providers.alpha_vantage import AlphaVantageMarketDataProvider
from providers.base import MarketDataProvider
from providers.fallback import MarketDataProviderChain
from providers.tiingo import TiingoMarketDataProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_price_data")

TICKERS = ["NVDA", "AMD", "MU", "SNDK"]
MARKET_TICKER = "SPY"
SECTOR_TICKER = "SOXX"
PRICE_HISTORY_START = date(2007, 1, 1)
SNAPSHOT_SOURCE = "price_backfill"


def build_provider_chain() -> MarketDataProvider:
    settings = get_settings()
    providers: list[tuple[str, MarketDataProvider]] = []
    if settings.tiingo_api_key:
        providers.append(("tiingo", TiingoMarketDataProvider(api_key=settings.tiingo_api_key)))
    if settings.alpha_vantage_api_key:
        av = AlphaVantageMarketDataProvider(api_key=settings.alpha_vantage_api_key)
        providers.append(("alpha_vantage", av))
    if not providers:
        raise RuntimeError(
            "no market data provider configured — set TIINGO_API_KEY or ALPHA_VANTAGE_API_KEY"
        )
    return MarketDataProviderChain(providers)


def _ingest_price_bars(db: Session, market: MarketDataProvider, ticker: str) -> int:
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    bars = market.get_daily_bars(ticker, PRICE_HISTORY_START, date.today())
    count = 0
    for bar in bars:
        row = (
            db.query(PriceBar)
            .filter_by(
                ticker=ticker, trade_date=bar.trade_date, source_provider=bar.source_provider
            )
            .one_or_none()
        )
        if row is None:
            row = PriceBar(
                ticker=ticker,
                company_id=company.id if company else None,
                trade_date=bar.trade_date,
                source_provider=bar.source_provider,
            )
            db.add(row)
        row.open, row.high, row.low, row.close, row.volume = (
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
        )
        row.retrieved_at = bar.retrieved_at
        count += 1
    db.commit()
    log.info("  %s: %d daily bars (%s)", ticker, count, bars[0].source_provider if bars else "n/a")
    return count


def _load_close_series(db: Session, ticker: str) -> dict[date, Decimal]:
    rows = db.query(PriceBar.trade_date, PriceBar.close).filter(PriceBar.ticker == ticker)
    # If both providers ever contributed rows for the same ticker, prefer
    # whichever trade_date has data — dict comprehension keeps the last
    # writer, which is fine since bars agree closely across providers.
    return dict(rows.all())


def _backfill_snapshots(
    db: Session,
    company: Company,
    ticker_bars: dict[date, Decimal],
    market_bars: dict[date, Decimal],
    sector_bars: dict[date, Decimal],
) -> int:
    events = (
        db.query(EarningsEvent)
        .filter(EarningsEvent.company_id == company.id, EarningsEvent.date_confirmed.is_(True))
        .all()
    )
    updated = 0
    for event in events:
        moves = price_reaction_moves(ticker_bars, event.earnings_date)
        if moves["close_price_before"] is None:
            continue
        as_of_date, stock_price = moves["close_price_before"]

        reaction = db.query(PriceReaction).filter_by(earnings_event_id=event.id).one_or_none()
        if reaction is None:
            reaction = PriceReaction(earnings_event_id=event.id)
            db.add(reaction)
        reaction.close_price_before = stock_price
        reaction.next_day_close = moves["next_day_close"][1] if moves["next_day_close"] else None
        reaction.five_day_close = moves["five_day_close"][1] if moves["five_day_close"] else None
        reaction.next_day_move_pct = moves["next_day_move_pct"]
        reaction.five_day_move_pct = moves["five_day_move_pct"]
        reaction.source_provider = SNAPSHOT_SOURCE
        reaction.retrieved_at = datetime.now(UTC)

        snapshot = (
            db.query(EarningsExpectationSnapshot)
            .filter_by(earnings_event_id=event.id, source_provider=SNAPSHOT_SOURCE)
            .one_or_none()
        )
        if snapshot is None:
            snapshot = EarningsExpectationSnapshot(
                earnings_event_id=event.id, source_provider=SNAPSHOT_SOURCE
            )
            db.add(snapshot)
        snapshot.snapshot_timestamp = datetime.combine(as_of_date, time(20, 0), tzinfo=UTC)
        snapshot.stock_price = stock_price
        snapshot.sector = company.sector
        snapshot.recent_return_5d = _safe_return(ticker_bars, as_of_date, 5)
        snapshot.recent_return_20d = _safe_return(ticker_bars, as_of_date, 20)
        snapshot.sector_return = _safe_return(sector_bars, as_of_date, 20)
        snapshot.market_return = _safe_return(market_bars, as_of_date, 20)
        snapshot.retrieved_at = datetime.now(UTC)
        updated += 1
    db.commit()
    return updated


def _safe_return(bars: dict[date, Decimal], as_of: date, lookback: int) -> Decimal | None:
    try:
        return recent_return(bars, as_of, lookback)
    except InsufficientPriceHistory:
        return None


def main() -> None:
    market = build_provider_chain()
    db = SessionLocal()
    try:
        log.info("=== reference series ===")
        _ingest_price_bars(db, market, MARKET_TICKER)
        _ingest_price_bars(db, market, SECTOR_TICKER)
        market_bars = _load_close_series(db, MARKET_TICKER)
        sector_bars = _load_close_series(db, SECTOR_TICKER)

        for ticker in TICKERS:
            log.info("=== %s ===", ticker)
            company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
            if company is None:
                log.warning("  no company row for %s — run bootstrap_phase1 first", ticker)
                continue
            _ingest_price_bars(db, market, ticker)
            ticker_bars = _load_close_series(db, ticker)
            updated = _backfill_snapshots(db, company, ticker_bars, market_bars, sector_bars)
            log.info("  %s: %d expectation snapshots + price reactions backfilled", ticker, updated)

        log.info("price data backfill complete")
    finally:
        db.close()


if __name__ == "__main__":
    main()
