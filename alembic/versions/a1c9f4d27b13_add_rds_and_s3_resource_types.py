"""add rds_instance and s3_bucket resource types

Revision ID: a1c9f4d27b13
Revises: 08872b80af2e
Create Date: 2026-07-27 21:40:00.000000

Phase 4B introduces the `rds_idle`, `rds_stopped`, `s3_no_lifecycle`, and
`s3_incomplete_multipart` rules, whose resources are not any of the four types
the original CHECK constraint allowed. Widening that constraint is the entire
migration — no columns change.

SQLite cannot ALTER a constraint, so `batch_alter_table` copies the table,
rebuilds it with the new definition, and swaps it in. Every column, index, and
sibling constraint therefore has to be re-declared here: whatever this block
omits is silently absent from the rebuilt table.

`downgrade()` restores the four-value constraint and will FAIL if any
rds_instance or s3_bucket row exists by then — deliberately. Silently deleting
inventory rows to make a downgrade succeed would also orphan every finding
referencing them. Delete those rows yourself first if you really mean it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c9f4d27b13"
down_revision: Union[str, None] = "08872b80af2e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_RESOURCE_TYPES = ("ebs_volume", "elastic_ip", "ec2_instance", "ebs_snapshot")
NEW_RESOURCE_TYPES = (*OLD_RESOURCE_TYPES, "rds_instance", "s3_bucket")


def _resource_type_check(values: tuple[str, ...]) -> str:
    return "resource_type IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _columns_only_resources() -> sa.Table:
    """The `resources` table as columns and primary key, with NO constraints.

    Handed to batch_alter_table as `copy_from`, which suppresses reflection of
    the live table. That suppression is the point: `table_args` *adds to* a
    reflected definition rather than replacing it, so reflecting here would
    rebuild the table carrying both the old four-value CHECK and the new
    six-value one — and the old one would still reject every rds_instance row.

    Columns mirror ResourceModel exactly.
    """
    return sa.Table(
        "resources",
        sa.MetaData(),
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_arn", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("current_tags", sa.String(), nullable=False),
        sa.Column("lifecycle", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )


def _rebuild_resources(resource_types: tuple[str, ...]) -> None:
    """Recreate `resources` with exactly the two CHECK constraints named here."""
    with op.batch_alter_table(
        "resources",
        recreate="always",
        copy_from=_columns_only_resources(),
        table_args=(
            sa.CheckConstraint(
                "lifecycle IN ('active', 'deleted')", name="check_resource_lifecycle"
            ),
            sa.CheckConstraint(
                _resource_type_check(resource_types), name="check_resource_type"
            ),
        ),
    ):
        # No column operations — the rebuild itself is the migration.
        pass

    # copy_from also suppresses index reflection, so the unique index on
    # resource_id has to be re-created by hand. upsert_resource's
    # insert-or-refresh logic depends on that uniqueness.
    bind = op.get_bind()
    existing = {index["name"] for index in sa.inspect(bind).get_indexes("resources")}
    if "ix_resources_resource_id" not in existing:
        op.create_index(
            op.f("ix_resources_resource_id"), "resources", ["resource_id"], unique=True
        )


def upgrade() -> None:
    _rebuild_resources(NEW_RESOURCE_TYPES)


def downgrade() -> None:
    bind = op.get_bind()
    stranded = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM resources "
            "WHERE resource_type IN ('rds_instance', 's3_bucket')"
        )
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"Cannot downgrade: {stranded} resource(s) use rds_instance or s3_bucket, "
            "which the previous CHECK constraint forbids. Delete those resources and "
            "the findings referencing them first if this downgrade is intended."
        )
    _rebuild_resources(OLD_RESOURCE_TYPES)
