"""Six-cohort forward evidence: candidate-level observations, per-configuration
entry positions and settlements.

V4 activation phase (2026-09-02). Three additive tables:

* ``v4_shadow_candidate_observation`` -- one executable-quote observation
  per unique selected candidate and phase (ENTRY/EXIT), shared by every
  configuration that selected that candidate. Quote evidence is stored
  once per candidate, never once per configuration.
* ``v4_shadow_config_entry`` -- each configuration's frozen position
  (candidate, quantity, capital used, max risk, entry value) at the shared
  observation's prices. One per configuration result.
* ``v4_shadow_config_settlement`` -- each configuration's realized T+1
  result for that exact frozen position. One per configuration result.

All three are append-only under the existing reject_snapshot_update()
trigger. WRITTEN BY HAND (autogenerate proposes dropping runtime-created
structures in this project); no existing table is altered.

Revision ID: c1e5a7d93f20
Revises: b8d4f02a1c37
"""

import sqlalchemy as sa
from alembic import op

revision = "c1e5a7d93f20"
down_revision = "b8d4f02a1c37"
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "v4_shadow_candidate_observation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shadow_decision_id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=8), nullable=False),
        _ts("observed_at", nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("failure_category", sa.String(length=48), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("net_executable_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("market_data_quality", sa.String(length=24), nullable=True),
        sa.Column("source_provider", sa.String(length=64), nullable=True),
        _ts("earliest_leg_observed_at"),
        _ts("latest_leg_observed_at"),
        sa.Column("max_leg_timestamp_skew_seconds", sa.Numeric(18, 6), nullable=True),
        sa.Column("unique_contract_count", sa.Integer(), nullable=True),
        sa.Column("legs_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["shadow_decision_id"], ["v4_shadow_decision.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "shadow_decision_id", "candidate_id", "phase",
            name="uq_v4_shadow_candidate_observation_one_per_candidate_phase",
        ),
    )
    op.create_index(
        "ix_v4_shadow_candidate_observation_shadow_decision_id",
        "v4_shadow_candidate_observation", ["shadow_decision_id"],
    )

    op.create_table(
        "v4_shadow_config_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shadow_config_result_id", sa.Integer(), nullable=False),
        sa.Column("shadow_decision_id", sa.Integer(), nullable=False),
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
        _ts("observed_at", nullable=False),
        sa.Column("market_data_quality", sa.String(length=24), nullable=True),
        sa.Column("failure_category", sa.String(length=48), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("timing_policy_version", sa.String(length=64), nullable=True),
        sa.Column("engine_version", sa.String(length=48), nullable=True),
        sa.Column("configuration_version", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["shadow_config_result_id"], ["v4_shadow_config_result.id"]),
        sa.ForeignKeyConstraint(["shadow_decision_id"], ["v4_shadow_decision.id"]),
        sa.ForeignKeyConstraint(["candidate_observation_id"], ["v4_shadow_candidate_observation.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shadow_config_result_id", name="uq_v4_shadow_config_entry_one_per_config"),
    )
    for col in ("shadow_decision_id", "candidate_observation_id", "configuration_key", "status"):
        op.create_index(f"ix_v4_shadow_config_entry_{col}", "v4_shadow_config_entry", [col])

    op.create_table(
        "v4_shadow_config_settlement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shadow_config_result_id", sa.Integer(), nullable=False),
        sa.Column("shadow_decision_id", sa.Integer(), nullable=False),
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
        _ts("entry_observed_at"),
        _ts("settled_at", nullable=False),
        sa.Column("pricing_convention", sa.String(length=48), nullable=False),
        sa.Column("market_data_quality", sa.String(length=24), nullable=True),
        sa.Column("failure_category", sa.String(length=48), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["shadow_config_result_id"], ["v4_shadow_config_result.id"]),
        sa.ForeignKeyConstraint(["shadow_decision_id"], ["v4_shadow_decision.id"]),
        sa.ForeignKeyConstraint(["candidate_observation_id"], ["v4_shadow_candidate_observation.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shadow_config_result_id", name="uq_v4_shadow_config_settlement_one_per_config"),
    )
    for col in ("shadow_decision_id", "candidate_observation_id", "configuration_key", "status"):
        op.create_index(f"ix_v4_shadow_config_settlement_{col}", "v4_shadow_config_settlement", [col])

    for table in ("v4_shadow_candidate_observation", "v4_shadow_config_entry", "v4_shadow_config_settlement"):
        op.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();"
        )


def downgrade() -> None:
    for table in ("v4_shadow_config_settlement", "v4_shadow_config_entry", "v4_shadow_candidate_observation"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update ON {table};")
    for col in ("status", "configuration_key", "candidate_observation_id", "shadow_decision_id"):
        op.drop_index(f"ix_v4_shadow_config_settlement_{col}", "v4_shadow_config_settlement")
    op.drop_table("v4_shadow_config_settlement")
    for col in ("status", "configuration_key", "candidate_observation_id", "shadow_decision_id"):
        op.drop_index(f"ix_v4_shadow_config_entry_{col}", "v4_shadow_config_entry")
    op.drop_table("v4_shadow_config_entry")
    op.drop_index("ix_v4_shadow_candidate_observation_shadow_decision_id", "v4_shadow_candidate_observation")
    op.drop_table("v4_shadow_candidate_observation")
