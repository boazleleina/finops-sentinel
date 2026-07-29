"""add spend_snapshots table

Revision ID: b7e2f81c4a90
Revises: a1c9f4d27b13
Create Date: 2026-07-28 10:00:00.000000

Phase 4B's anomaly detection needs a daily history to compare today against,
and no existing table carries one: findings record what is open now, not what
was open last Tuesday.

`snapshot_date` is UNIQUE on purpose. The repository upserts by date, so two
scans on the same day collapse to one row — otherwise a day that happened to
be scanned five times would weigh five times as much in the trailing mean, and
the z-score would measure scan cadence rather than spend.

Unlike a1c9f4d27b13 this migration only CREATEs a table, so it needs none of
that revision's `copy_from` batch-rebuild machinery: nothing existing is being
altered, and both directions are lossless in schema terms. `downgrade()` does
drop the recorded history, which is the point of a downgrade here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2f81c4a90"
down_revision: Union[str, None] = "a1c9f4d27b13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "spend_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        # Stored as a string, matching SafeNumeric: SQLite has no Decimal, and
        # a float column would quietly round money.
        sa.Column("total_estimated_monthly_usd", sa.String(), nullable=False),
        sa.Column("open_findings", sa.Integer(), nullable=False),
        sa.Column("active_resources", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        op.f("ix_spend_snapshots_snapshot_date"),
        "spend_snapshots",
        ["snapshot_date"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_spend_snapshots_snapshot_date"), table_name="spend_snapshots")
    op.drop_table("spend_snapshots")
