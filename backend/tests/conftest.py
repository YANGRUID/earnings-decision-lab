import time
from collections.abc import Generator

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from db.session import engine
from models.app_provider_settings import AppProviderSettings
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.provider_health_event import ProviderHealthEvent


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handful of ingestion-adjacent functions call time.sleep to pace
    real Alpha Vantage API requests (see services/market_expectations.py) --
    no test should ever wait on a real clock for that, so this keeps the
    whole suite fast and deterministic regardless of which module adds one.
    """
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """A session bound to a connection whose transaction is always rolled
    back — tests hit the real (dockerized, local-only) Postgres schema
    without leaving any data behind.
    """
    connection = engine.connect()
    transaction = connection.begin()
    # A session-level rollback (e.g. after an IntegrityError) must not lose
    # the outer transaction, so the session runs inside a SAVEPOINT instead
    # of directly in it. See SQLAlchemy's "Joining a Session into an External
    # Transaction" recipe.
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def clean_provider_state(db_session: Session) -> Session:
    """Opt-in cleanup for the handful of tests that assert against a truly
    empty app_provider_settings / price_bar / options_snapshot /
    provider_health_event -- either a fresh-install invariant (the
    singleton settings row doesn't exist yet) or an exact "most recent
    successful ingest" value.

    ``db_session``'s own per-test rollback only undoes what *this* test
    writes -- it can't see, and can't clean up, rows a completely
    different, already-committed process wrote to this shared, dockerized
    dev Postgres instance before the test even started (e.g. the real
    scheduler/backend container, or a one-off ingestion script run
    against the same local DB). Those rows are real and outside any
    test's transaction, so plain rollback never removes them, and a
    handful of "fresh database" / "exact last-ingested value" assertions
    break once enough of them accumulate.

    Deleting them here is safe: every table below is a real leaf table
    with no incoming foreign keys (verified against the live schema), and
    the delete happens inside *this test's own* savepoint-wrapped
    transaction, which still rolls back at teardown exactly like every
    other db_session test -- so nothing is ever permanently lost, this
    just gives the one test that opts in a guaranteed-clean view for the
    duration of its own run.
    """
    for model in (AppProviderSettings, PriceBar, OptionsSnapshot, ProviderHealthEvent):
        db_session.execute(delete(model))
    db_session.flush()
    return db_session
