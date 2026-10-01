import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import models  # noqa: E402  (import for side effect: registers all models on Base.metadata)
from core.config import get_settings  # noqa: E402
from db.base import Base  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the .ini URL with the one derived from Settings (.env), so the
# database connection is configured in exactly one place.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata
assert models  # keep the import from being flagged as unused

# --- Objects this application does NOT own in Base.metadata -------------
#
# Each of these is created and owned by something other than a migration,
# so `alembic revision --autogenerate` sees it in the database, cannot find
# it in the metadata, and proposes a DROP. That has happened repeatedly and
# been caught by hand every time (see docs/engineering_decisions.md). The
# hook below makes the protection structural instead of a review habit --
# autogenerate now simply never considers these objects in either
# direction, so it can neither drop nor recreate them.
#
# This filter only affects what autogenerate WRITES. It does not change any
# existing revision, and a hand-written migration can still touch these
# objects deliberately.
_EXTERNALLY_MANAGED_TABLES = frozenset(
    {
        # APScheduler creates and migrates its own job store at runtime.
        "apscheduler_jobs",
        # LangGraph's Postgres checkpointer runs its own migrations via
        # PostgresSaver.setup() (Phase LG-1, 2026-10-01). These normally
        # live in the dedicated `langgraph` schema, which autogenerate
        # already ignores; named here as well so a future include_schemas
        # change cannot quietly reintroduce the drop.
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    }
)
_EXTERNALLY_MANAGED_INDEXES = frozenset(
    {
        # pgvector HNSW and Postgres full-text indexes, both created by
        # hand-written migrations using raw DDL that SQLAlchemy's Index
        # construct cannot express, so neither appears in the metadata.
        "ix_document_chunk_embedding_hnsw",
        "ix_document_chunk_text_fts",
    }
)
_EXTERNALLY_MANAGED_SCHEMAS = frozenset({"langgraph"})


def include_object(object_, name, type_, reflected, compare_to):  # noqa: ANN001, ANN201
    if getattr(object_, "schema", None) in _EXTERNALLY_MANAGED_SCHEMAS:
        return False
    if type_ == "table" and name in _EXTERNALLY_MANAGED_TABLES:
        return False
    if type_ == "index" and name in _EXTERNALLY_MANAGED_INDEXES:
        return False
    return True

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
