"""LangGraph checkpointing, on Postgres, in its own schema.

Why Postgres and not Redis (requirement 37): this stack already runs
Postgres and already depends on ``psycopg[binary]``. Redis would be a new
service to deploy, monitor and back up, bought for no capability the
research graph needs.

Why its own schema (requirements 38, 88): a checkpoint is *operational*
state -- "this run got as far as merge_evidence" -- and must never be
mistaken for financial evidence. Keeping the four checkpointer tables in a
dedicated ``langgraph`` schema makes that separation physical rather than
conventional: a query against ``public`` cannot reach them by accident,
and ``migrations/env.py`` ignores the schema so autogenerate can neither
drop nor recreate them.

Why not an Alembic migration (requirement 87): the checkpointer runs its
own versioned migrations through ``PostgresSaver.setup()``. Re-declaring
its tables in Alembic would be two owners for one schema -- exactly the
duplication requirement 87 warns against -- and the first upstream schema
change would put them out of sync. ``setup()`` is idempotent and is called
once at startup.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

log = logging.getLogger("agents.graph.checkpoint")

#: The dedicated Postgres schema. Not configurable: an operator who moved
#: it would silently escape the autogenerate protection in migrations/env.py.
LANGGRAPH_SCHEMA = "langgraph"

#: The tables PostgresSaver owns. Listed for the status endpoint and for
#: the test that asserts none of them lands in the domain schema.
CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)


def libpq_url(database_url: str) -> str:
    """``postgresql+psycopg://...`` -> ``postgresql://...``.

    The application's URL is a SQLAlchemy DSN; psycopg needs a libpq one.
    Converted here rather than configured twice, so there stays one
    database URL in this project (core/config.py).
    """
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _with_search_path(url: str) -> str:
    """Pins the connection's ``search_path`` to the LangGraph schema.

    PostgresSaver's own DDL is unqualified (``CREATE TABLE IF NOT EXISTS
    checkpoints``), so the search path is what decides where its tables
    land. ``public`` is kept on the path after it so shared extensions
    still resolve.
    """
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}options=-csearch_path%3D{LANGGRAPH_SCHEMA}%2Cpublic"


@contextmanager
def postgres_checkpointer(database_url: str, *, setup: bool = True) -> Iterator[object]:
    """A ``PostgresSaver`` scoped to one caller, with its schema ensured.

    ``setup=False`` skips the migration run for a caller that knows the
    schema is already current -- it is idempotent but it does round-trip
    to the database, which is wasted work inside a request.
    """
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    base = libpq_url(database_url)
    with psycopg.connect(base, autocommit=True) as bootstrap:
        bootstrap.execute(f'CREATE SCHEMA IF NOT EXISTS "{LANGGRAPH_SCHEMA}"')

    with PostgresSaver.from_conn_string(_with_search_path(base)) as saver:
        if setup:
            saver.setup()
        yield saver


@dataclass(frozen=True)
class CheckpointStatus:
    """What Operations needs to know about checkpointing, without exposing
    any run's contents."""

    available: bool
    schema: str
    tables_present: list[str]
    #: Set when ``available`` is False. Already a short, safe message --
    #: never a DSN (which would carry a password in its userinfo).
    reason: str | None = None


def checkpoint_status(database_url: str) -> CheckpointStatus:
    """Read-only. Reports whether the checkpointer's own tables exist yet,
    which is the honest difference between "resume is available" and
    "nothing has ever been checkpointed"."""
    import psycopg

    from observability.redact import redact

    try:
        with psycopg.connect(libpq_url(database_url), autocommit=True) as conn:
            rows = conn.execute(
                "select tablename from pg_tables where schemaname = %s order by tablename",
                (LANGGRAPH_SCHEMA,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 — status must never raise
        return CheckpointStatus(
            available=False,
            schema=LANGGRAPH_SCHEMA,
            tables_present=[],
            reason=redact(str(exc)),
        )
    present = [r[0] for r in rows]
    missing = [t for t in CHECKPOINT_TABLES if t not in present]
    return CheckpointStatus(
        available=not missing,
        schema=LANGGRAPH_SCHEMA,
        tables_present=present,
        reason=(
            f"checkpointer not initialised yet (missing {', '.join(missing)})"
            if missing
            else None
        ),
    )
