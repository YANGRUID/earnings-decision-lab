from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session

from db.session import engine


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
