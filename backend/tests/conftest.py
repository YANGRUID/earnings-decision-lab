import os
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from core.config import Settings
from models.app_provider_settings import AppProviderSettings
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.provider_health_event import ProviderHealthEvent

# IBKR TWS Migration, production cutover (2026-09-01) -- a real,
# live-discovered gap this cutover's own post-cutover test run surfaced:
# with the real .env now set to IBKR_PROVIDER=tws (the correct production
# config), any test that builds a Settings()-backed app/lifespan without
# its own explicit override -- e.g. every `with TestClient(app) as
# client:` fixture across this suite -- silently inherited that real
# value too, and FastAPI's own lifespan (api/main.py) then opened a real
# socket to the real, live IB Gateway on every such fixture's setup.
# Slow (many bounded real connect attempts), non-deterministic (depends
# on a live external system reachable from a test process at all -- see
# this file's own "real application state must never be reachable from a
# test process" precedent immediately below, same principle, an even
# more serious instance of it), and a genuine risk of a real client-id
# collision (error 326, confirmed live and separately fixed today, see
# services/provider_test_connection.py) against the actual, currently-
# running production backend's own persistent TWS connection. Almost no
# test in this suite cares which IBKR transport is nominally configured
# -- the ones that do already set it explicitly and locally (e.g.
# test_services_provider_test_connection.py's own `_settings(ibkr_
# provider="tws")` helper). setdefault, not a hard overwrite: an explicit
# `IBKR_PROVIDER=... pytest` invocation (e.g. this exact diagnosis) still
# wins.
os.environ.setdefault("IBKR_PROVIDER", "web")

# --------------------------------------------------------------------------
# Pre-live hardening (2026-08-25): pytest used to import `engine` directly
# from db.session -- the exact same engine, bound to the exact same
# DATABASE_URL, the real running application uses. That worked (every
# `db_session` test rolls back its own transaction) right up until the
# real application itself committed a real row to a table a test also
# queried mid-suite (a real SchedulerRun from the live backend's own
# scheduler -- see services/scheduler_run_tracking.py -- collided with
# test_services_scheduler.py's own bare `.one()` assertions). Real
# application state must never be reachable from a test process at all,
# not just "safe in practice because nobody hits the unlucky timing" --
# so pytest now gets its own, completely separate engine, and refuses to
# start at all if that engine ever resolves to the same database the
# real app uses.
#
# Reuses this project's own existing disposable-Postgres pattern rather
# than inventing a second one: `edl-test-db` (port 5434, same
# `earnings_decision_lab` database name, different container) already
# exists and is already documented as shared with the Playwright E2E
# suite (see frontend/playwright.config.ts, frontend/e2e/global-
# teardown.ts's own docstring) -- pytest now actually uses it too,
# matching what those comments already claimed.
_DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://postgres:change_me@localhost:5434/earnings_decision_lab"
)
_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", _DEFAULT_TEST_DATABASE_URL)


def _database_identity(url: str) -> tuple[str | None, int | None, str | None]:
    parsed = make_url(url)
    return (parsed.host, parsed.port, parsed.database)


_real_database_url = Settings().database_url
if _database_identity(_TEST_DATABASE_URL) == _database_identity(_real_database_url):
    raise RuntimeError(
        "Refusing to run tests: TEST_DATABASE_URL resolves to the exact same "
        f"database the real application uses ({_real_database_url!r}). Tests "
        "must never run against the real dev/live database.\n\n"
        "Fix: point TEST_DATABASE_URL at a disposable test Postgres instead, "
        "e.g. this project's own edl-test-db container (port 5434):\n\n"
        "  docker run -d --name edl-test-db -p 5434:5432 "
        "-e POSTGRES_DB=earnings_decision_lab -e POSTGRES_USER=postgres "
        "-e POSTGRES_PASSWORD=change_me pgvector/pgvector:pg16\n\n"
        "(or `docker start edl-test-db` if it already exists but is stopped)."
    )

engine = create_engine(_TEST_DATABASE_URL, pool_pre_ping=True, future=True)


def _migrate_test_database() -> None:
    """Applies migrations to the disposable test database, once per test
    session -- `alembic upgrade head` is a cheap no-op when already
    current, so this is safe to run unconditionally rather than trusting
    edl-test-db to have been migrated by hand before the suite runs (it
    was observed stale -- several migrations behind head -- the first
    time this was wired up)."""
    backend_dir = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        env={**os.environ, "DATABASE_URL": _TEST_DATABASE_URL},
        check=True,
        capture_output=True,
        text=True,
    )


try:
    with engine.connect() as _conn:
        _conn.execute(text("SELECT 1"))
except Exception as exc:  # pragma: no cover -- environment/setup error, not a test failure
    raise RuntimeError(
        f"Could not connect to the test database ({_TEST_DATABASE_URL!r}). "
        "Start it before running pytest, e.g.:\n\n"
        "  docker start edl-test-db\n\n"
        "(or create it -- see the RuntimeError raised above this one for the "
        "full `docker run` command -- if it doesn't exist yet)."
    ) from exc

_migrate_test_database()


# ---------------------------------------------------------------------------
# Production-write containment (added 2026-09-01 after a real incident).
#
# The guard further up only proves the FIXTURE engine is not the real
# database. It does nothing about production code that opens a session of
# its own: db/session.py builds `engine`/`SessionLocal` (and the separate
# `scheduler_engine`/`SchedulerSessionLocal`) at import time from
# get_settings().database_url -- the REAL one. Any test that calls a
# scheduler job body, worker loop, or ingestion entry point therefore ran
# `SessionLocal()` against the live database and committed to it.
#
# That is not hypothetical: tests/test_v4_5_shadow_scheduler.py invokes
# run_v4_forward_window_job() directly, and
# wrote 44 rows into the production scheduler_run table (24 SKIPPED plus 20
# with error_summary "boom") before this was noticed. The tests still
# "passed" -- one of them even asserted on the test-database session while
# the job wrote to production, so the assertion was true and meaningless.
#
# Rebinding here, at conftest import time, means every later
# `from db.session import SessionLocal` resolves to the test engine. The
# sys.modules sweep repoints any holder that already imported the real one.
# ---------------------------------------------------------------------------
import db.session as _db_session  # noqa: E402

_TestSessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, future=True
)
_PRODUCTION_FACTORIES = {
    id(_db_session.SessionLocal),
    id(_db_session.SchedulerSessionLocal),
}
_PRODUCTION_ENGINES = {id(_db_session.engine), id(_db_session.scheduler_engine)}

_db_session.engine = engine
_db_session.scheduler_engine = engine
_db_session.SessionLocal = _TestSessionLocal
_db_session.SchedulerSessionLocal = _TestSessionLocal

for _mod in list(sys.modules.values()):
    if _mod is None or _mod is _db_session:
        continue
    for _attr in ("SessionLocal", "SchedulerSessionLocal", "engine", "scheduler_engine"):
        _held = getattr(_mod, _attr, None)
        if _held is None:
            continue
        if id(_held) in _PRODUCTION_FACTORIES:
            setattr(_mod, _attr, _TestSessionLocal)
        elif id(_held) in _PRODUCTION_ENGINES:
            setattr(_mod, _attr, engine)


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
    back — tests hit the disposable test Postgres schema (edl-test-db,
    never the real dev/live database -- see the module-level guard above)
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
    different, already-committed process wrote to this disposable test
    Postgres instance before the test even started (this database is
    shared with the Playwright E2E suite -- see frontend/playwright.
    config.ts -- and with every other pytest run against it). Those rows
    are real and outside any test's transaction, so plain rollback never
    removes them, and a handful of "fresh database" / "exact
    last-ingested value" assertions break once enough of them accumulate.

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


# --------------------------------------------------------------------------
# IBKR TWS Migration, post-cutover cleanup A8 (2026-09-01) -- a structural
# guarantee that no test ever opens a real socket to a live IB Gateway.
#
# The env default above (IBKR_PROVIDER=web) stops the app lifespan from
# *choosing* the TWS transport, which is what actually bit during the
# cutover -- but on its own it is only a default, and a default is a
# fragile thing to rest a live-brokerage-connection guarantee on: any
# test that constructs Settings with an explicit ibkr_provider="tws", or
# any future code path that reaches TWSConnectionManager directly, would
# silently sail past it and dial the real Gateway at a production client
# id.
#
# So the socket itself is closed off here, at the one place every real
# connection must pass through (EClient.connect, inherited by
# TWSConnectionManager). Instead of silently coercing behavior, this
# FAILS LOUDLY and names the problem -- a test that trips it has a real
# bug worth seeing, not something to paper over.
#
# Nothing legitimate is broken by this: the tests that genuinely exercise
# TWSConnectionManager already mock the socket layer per-instance (see
# test_providers_ibkr_tws_client.py::_mock_socket_layer, which assigns
# manager.connect on the instance, shadowing this class-level guard) or
# patch connect_and_start outright (test_services_system_status.py).
#
# A deliberate live-integration run can opt in explicitly with
# ALLOW_LIVE_IBKR_TESTS=1 -- never implicitly, and never by default.
ALLOW_LIVE_IBKR_ENV_VAR = "ALLOW_LIVE_IBKR_TESTS"


@pytest.fixture(autouse=True)
def _block_live_ibkr_sockets(monkeypatch):
    if os.environ.get(ALLOW_LIVE_IBKR_ENV_VAR) == "1":
        return

    from providers.ibkr_tws_client import TWSConnectionManager

    def _refuse(self, *args, **kwargs):
        raise AssertionError(
            "A test attempted a REAL socket connection to IB Gateway/TWS "
            f"({args[:2] or 'host/port unknown'}). Tests must use a fake/mocked "
            "connection -- see tests/test_providers_ibkr_tws_options.py's own "
            "_FakeConnection, or mock the socket layer per-instance as "
            "test_providers_ibkr_tws_client.py does. If this is a deliberate "
            f"live-integration run, set {ALLOW_LIVE_IBKR_ENV_VAR}=1 explicitly."
        )

    monkeypatch.setattr(TWSConnectionManager, "connect", _refuse, raising=False)
