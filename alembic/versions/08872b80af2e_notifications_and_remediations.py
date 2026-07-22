"""notifications_and_remediations

Revision ID: 08872b80af2e
Revises: c447645246ac
Create Date: 2026-07-21 21:35:22.577846

Satellite tables off findings: one row per notification sent and one row per
remediation attempt (dry-runs and retries included). snapshot_id — the
recovery path for volume deletions — is durably recorded in remediations.detail.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "08872b80af2e"
down_revision: Union[str, None] = "c447645246ac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("finding_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("message_ref", sa.String(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notifications_finding_id"), "notifications", ["finding_id"], unique=False
    )
    op.create_table(
        "remediations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("finding_id", sa.String(), nullable=False),
        sa.Column("playbook", sa.String(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("result", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_remediations_finding_id"), "remediations", ["finding_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_remediations_finding_id"), table_name="remediations")
    op.drop_table("remediations")
    op.drop_index(op.f("ix_notifications_finding_id"), table_name="notifications")
    op.drop_table("notifications")
