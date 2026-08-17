import time
from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session

from db.session import engine


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
