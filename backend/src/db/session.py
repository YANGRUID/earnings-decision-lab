from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import get_settings

settings = get_settings()

# Sized above SQLAlchemy's defaults (5/10 = 15 total) -- this is the
# API-request-facing engine only (see get_db()/FastAPI's Depends(get_db)),
# not shared with the scheduler (see scheduler_engine below). 30 total
# gives real headroom for sustained Operations Monitor polling and other
# API traffic without any of it competing with the scheduler for a
# connection.
engine = create_engine(
    settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20, future=True
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


# A second, independent engine against the exact same Postgres database
# (not a second database -- see PRE_LIVE_HARDENING notes in services/
# scheduler.py) reserved for the scheduler's own use: APScheduler's
# SQLAlchemyJobStore and every job body's own SessionLocal() call.
#
# Real, empirically-observed failure this fixes: with one engine shared
# between API requests and the scheduler, the Operations Monitor page's
# own polling alone was enough to exhaust the shared pool and starve a
# scheduled job of a connection for several fire cycles -- next_run_time
# kept advancing (APScheduler still believed it submitted the job) while
# no SchedulerRun was ever recorded and nothing was logged anywhere. A
# bigger shared pool (the `engine` above) reduces how often that can
# happen but doesn't eliminate it: enough concurrent slow API requests
# (an outbound IBKR call took over 4 seconds during testing) can still
# saturate any fixed-size shared pool. A genuinely separate engine means
# the scheduler's connection budget is never a function of how much API
# traffic exists at the same moment, at the small, fixed cost of a
# second modest connection pool against the same database server --
# Postgres itself has no problem with two client pools, and this
# introduces no new service, no new database, and no duplicated business
# logic (every job body is completely unchanged; only which SessionLocal
# it calls changes).
scheduler_engine = create_engine(
    settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5, future=True
)

SchedulerSessionLocal = sessionmaker(
    bind=scheduler_engine, autoflush=False, autocommit=False, future=True
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
