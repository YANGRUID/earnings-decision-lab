"""v4.2 challenger forward outcomes: entry, settlement and expiry provenance

Additive only, and hand-written for the same reason the previous challenger
migration was: ``alembic revision --autogenerate`` has, in this repository,
proposed dropping ``apscheduler_jobs``, the RAG HNSW index and the FTS index.
Every statement below was written and audited by hand. Nothing here drops,
alters or renames anything belonging to V4.1 evidence, research, the calendar,
the providers or the scheduler.

What arrives:

1. ``v4_2_challenger_candidate_observation`` -- ONE executable quote
   observation per (challenger decision, candidate, phase). This is the table
   that makes "six configurations, one quote acquisition" structural rather
   than a convention: configurations reference this row, they do not each
   carry their own copy of the prices.

2. ``v4_2_challenger_config_entry`` -- a configuration's frozen hypothetical
   position: which candidate, how many contracts, capital used, entry value.
   NO_ACTION configurations get no row at all, so a declined configuration can
   never acquire a phantom position or a P&L.

3. ``v4_2_challenger_config_settlement`` -- a configuration's realized T+1
   result. Append-only with a PARTIAL unique index on ``status = 'SETTLED'``:
   a configuration may accumulate any number of immutable failed attempts, and
   at most one settlement of record. Exactly the shape V4.1 arrived at after
   the 2026-09-04 recovery, for the same reason.

4. Expiry provenance on ``v4_2_challenger_candidate`` and a chain-snapshot
   link plus multi-expiry counters on ``v4_2_challenger_decision``. All
   nullable and additive; existing rows (there are none in production) would
   read NULL, which is honest -- the single-expiry challenger genuinely had no
   per-expiry implied move.

Revision ID: f7a9c1e3d5b8
Revises: e5b7c9d1f3a4
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f7a9c1e3d5b8"
down_revision = "e5b7c9d1f3a4"
branch_labels = None
depends_on = None

_IMMUTABLE = (
    "v4_2_challenger_candidate_observation",
    "v4_2_challenger_config_entry",
    "v4_2_challenger_config_settlement",
)


def upgrade() -> None:
    # ---- 1. shared candidate quote observation ---------------------------
    op.create_table(
        "v4_2_challenger_candidate_observation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("challenger_decision_id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=8), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("failure_category", sa.String(length=48), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("net_executable_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("pricing_convention", sa.String(length=48), nullable=False),
        sa.Column("pricing_method", sa.String(length=96), nullable=True),
        sa.Column("market_data_quality", sa.String(length=24), nullable=True),
        sa.Column("source_provider", sa.String(length=64), nullable=True),
        sa.Column("earliest_leg_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_leg_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_leg_timestamp_skew_seconds", sa.Numeric(18, 6), nullable=True),
        sa.Column("unique_contract_count", sa.Integer(), nullable=True),
        # How many of this observation's contracts were priced from evidence
        # the CONTROL had already acquired in the same window. The claim that
        # the challenger adds no quote sweep is measured here, per observation.
        sa.Column("contracts_shared_with_control", sa.Integer(), nullable=True),
        sa.Column("legs_json", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["challenger_decision_id"], ["v4_2_challenger_decision.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "challenger_decision_id",
            "candidate_id",
            "phase",
            name="uq_v4_2_challenger_observation_one_per_candidate_phase",
        ),
    )
    op.create_index(
        "ix_v4_2_challenger_observation_decision",
        "v4_2_challenger_candidate_observation",
        ["challenger_decision_id"],
    )

    # ---- 2. per-configuration frozen position ----------------------------
    op.create_table(
        "v4_2_challenger_config_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("challenger_config_result_id", sa.Integer(), nullable=False),
        sa.Column("challenger_decision_id", sa.Integer(), nullable=False),
        sa.Column("candidate_observation_id", sa.Integer(), nullable=False),
        sa.Column("configuration_key", sa.String(length=48), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("standardized_capital", sa.Numeric(18, 2), nullable=False),
        sa.Column("capital_used", sa.Numeric(18, 6), nullable=True),
        sa.Column("max_risk_per_contract", sa.Numeric(18, 6), nullable=True),
        sa.Column("max_risk_used", sa.Numeric(18, 6), nullable=True),
        sa.Column("entry_net_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("pricing_convention", sa.String(length=48), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_data_quality", sa.String(length=24), nullable=True),
        sa.Column("failure_category", sa.String(length=48), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        # Frozen position identity: the exact contracts, so settlement can
        # resolve by conId and can never re-select a strike.
        sa.Column("frozen_legs_json", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("expiration", sa.Date(), nullable=True),
        sa.Column("expiry_ladder_position", sa.Integer(), nullable=True),
        sa.Column("entry_dte", sa.Integer(), nullable=True),
        sa.Column("dte_at_settlement", sa.Integer(), nullable=True),
        sa.Column("timing_policy_version", sa.String(length=64), nullable=True),
        sa.Column("methodology_version", sa.String(length=64), nullable=True),
        sa.Column("configuration_version", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["challenger_config_result_id"], ["v4_2_challenger_config_result.id"]
        ),
        sa.ForeignKeyConstraint(["challenger_decision_id"], ["v4_2_challenger_decision.id"]),
        sa.ForeignKeyConstraint(
            ["candidate_observation_id"], ["v4_2_challenger_candidate_observation.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # One entry OF RECORD per configuration result. Unlike settlement there is
    # no retry concept at entry: the entry instant is the decision window and
    # cannot be revisited, so a failed entry is terminal and still unique.
    op.create_index(
        "uq_v4_2_challenger_config_entry_one_per_config",
        "v4_2_challenger_config_entry",
        ["challenger_config_result_id"],
        unique=True,
    )
    op.create_index(
        "ix_v4_2_challenger_config_entry_decision",
        "v4_2_challenger_config_entry",
        ["challenger_decision_id"],
    )

    # ---- 3. per-configuration realized outcome ---------------------------
    op.create_table(
        "v4_2_challenger_config_settlement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("challenger_config_result_id", sa.Integer(), nullable=False),
        sa.Column("challenger_decision_id", sa.Integer(), nullable=False),
        sa.Column("challenger_config_entry_id", sa.Integer(), nullable=False),
        sa.Column("candidate_observation_id", sa.Integer(), nullable=True),
        sa.Column("configuration_key", sa.String(length=48), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("standardized_capital", sa.Numeric(18, 2), nullable=False),
        sa.Column("capital_used", sa.Numeric(18, 6), nullable=True),
        sa.Column("entry_net_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("exit_net_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(18, 6), nullable=True),
        sa.Column("return_on_standardized_capital", sa.Numeric(18, 8), nullable=True),
        sa.Column("return_on_capital_used", sa.Numeric(18, 8), nullable=True),
        sa.Column("entry_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pricing_convention", sa.String(length=48), nullable=False),
        # The SAME vocabulary the control uses: EXECUTABLE_BID/EXECUTABLE_ASK/
        # MARKET_CLOSE_FALLBACK/EXPIRATION_INTRINSIC_AT_CLOSE. The challenger
        # gets no pricing hierarchy of its own.
        sa.Column("pricing_method", sa.String(length=96), nullable=True),
        sa.Column("settlement_grade", sa.String(length=32), nullable=True),
        sa.Column("recovery_provenance", sa.String(length=48), nullable=True),
        sa.Column("supersedes_settlement_id", sa.Integer(), nullable=True),
        sa.Column("market_data_quality", sa.String(length=24), nullable=True),
        sa.Column("failure_category", sa.String(length=48), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("timing_policy_version", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["challenger_config_result_id"], ["v4_2_challenger_config_result.id"]
        ),
        sa.ForeignKeyConstraint(["challenger_decision_id"], ["v4_2_challenger_decision.id"]),
        sa.ForeignKeyConstraint(
            ["challenger_config_entry_id"], ["v4_2_challenger_config_entry.id"]
        ),
        sa.ForeignKeyConstraint(
            ["candidate_observation_id"], ["v4_2_challenger_candidate_observation.id"]
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_settlement_id"], ["v4_2_challenger_config_settlement.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Many immutable attempts; at most one settlement of record. Enforced by
    # the database rather than by the service that writes it.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_v4_2_challenger_settlement_one_settled_per_config
        ON v4_2_challenger_config_settlement (challenger_config_result_id)
        WHERE status = 'SETTLED'
        """
    )
    op.create_index(
        "ix_v4_2_challenger_settlement_decision",
        "v4_2_challenger_config_settlement",
        ["challenger_decision_id"],
    )
    op.create_index(
        "ix_v4_2_challenger_settlement_status",
        "v4_2_challenger_config_settlement",
        ["status"],
    )

    # ---- 4. expiry provenance (additive, nullable) -----------------------
    # Section 31: a candidate must carry the expiry context it was actually
    # constructed in, not one inherited from the nearest expiry.
    op.add_column(
        "v4_2_challenger_candidate",
        sa.Column("expiry_implied_move_pct", sa.Numeric(18, 8), nullable=True),
    )
    op.add_column(
        "v4_2_challenger_candidate",
        sa.Column("expiry_implied_move_source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "v4_2_challenger_candidate",
        sa.Column("chain_metadata_snapshot_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_v4_2_challenger_candidate_chain_snapshot",
        "v4_2_challenger_candidate",
        "v4_chain_metadata_snapshot",
        ["chain_metadata_snapshot_id"],
        ["id"],
    )
    op.add_column(
        "v4_2_challenger_decision",
        sa.Column("chain_metadata_snapshot_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_v4_2_challenger_decision_chain_snapshot",
        "v4_2_challenger_decision",
        "v4_chain_metadata_snapshot",
        ["chain_metadata_snapshot_id"],
        ["id"],
    )
    op.add_column(
        "v4_2_challenger_decision",
        sa.Column("expiries_considered", sa.Integer(), nullable=True),
    )
    op.add_column(
        "v4_2_challenger_decision",
        sa.Column("multi_expiry_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "v4_2_challenger_decision",
        sa.Column("entry_status", sa.String(length=24), nullable=True),
    )

    # Append-only, using the trigger function an earlier migration owns.
    for table in _IMMUTABLE:
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
            """
        )


def downgrade() -> None:
    # Refuse to discard realized challenger evidence. A downgrade that
    # silently deleted settled forward outcomes would destroy exactly the
    # record this phase exists to accumulate.
    bind = op.get_bind()
    settled = bind.exec_driver_sql(
        "SELECT count(*) FROM v4_2_challenger_config_settlement"
    ).scalar()
    if settled:
        raise RuntimeError(
            f"refusing to downgrade: {settled} challenger settlement row(s) exist. "
            "Export them first; this migration will not delete realized forward evidence."
        )

    for table in _IMMUTABLE:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update ON {table}")
    op.drop_column("v4_2_challenger_decision", "entry_status")
    op.drop_column("v4_2_challenger_decision", "multi_expiry_status")
    op.drop_column("v4_2_challenger_decision", "expiries_considered")
    op.drop_constraint(
        "fk_v4_2_challenger_decision_chain_snapshot", "v4_2_challenger_decision", type_="foreignkey"
    )
    op.drop_column("v4_2_challenger_decision", "chain_metadata_snapshot_id")
    op.drop_constraint(
        "fk_v4_2_challenger_candidate_chain_snapshot",
        "v4_2_challenger_candidate",
        type_="foreignkey",
    )
    op.drop_column("v4_2_challenger_candidate", "chain_metadata_snapshot_id")
    op.drop_column("v4_2_challenger_candidate", "expiry_implied_move_source")
    op.drop_column("v4_2_challenger_candidate", "expiry_implied_move_pct")
    op.drop_table("v4_2_challenger_config_settlement")
    op.drop_table("v4_2_challenger_config_entry")
    op.drop_table("v4_2_challenger_candidate_observation")
    # reject_snapshot_update() stays: it is owned by the migration that
    # created it and every other evidence table still uses it.
