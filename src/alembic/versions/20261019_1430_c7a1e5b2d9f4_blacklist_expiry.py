"""Blacklist reason and expiry

Revision ID: c7a1e5b2d9f4
Revises: b1345f835923
Create Date: 2026-10-19 14:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c7a1e5b2d9f4"
down_revision: Union[str, None] = "b1345f835923"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows get no reason or expiry, so they stay blacklisted for good
    with op.batch_alter_table("StreamBlacklistRelation", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reason", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("blacklisted_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("released_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("strikes", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.create_index(
            "ix_streamblacklistrelation_expires_at", ["expires_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("StreamBlacklistRelation", schema=None) as batch_op:
        batch_op.drop_index("ix_streamblacklistrelation_expires_at")
        batch_op.drop_column("strikes")
        batch_op.drop_column("released_at")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("blacklisted_at")
        batch_op.drop_column("reason")
