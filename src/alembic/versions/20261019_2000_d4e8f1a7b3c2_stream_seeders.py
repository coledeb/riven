"""Stream seeders

Revision ID: d4e8f1a7b3c2
Revises: c7a1e5b2d9f4
Create Date: 2026-10-19 20:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d4e8f1a7b3c2"
down_revision: Union[str, None] = "c7a1e5b2d9f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing streams get no count until they are scraped again
    with op.batch_alter_table("Stream", schema=None) as batch_op:
        batch_op.add_column(sa.Column("seeders", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("Stream", schema=None) as batch_op:
        batch_op.drop_column("seeders")
