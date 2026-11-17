"""feed_cursors for the Polygon trade feed; drop the Data API poller's hwm_ts

Revision ID: e7b2c4d91f05
Revises: d1f3a9c2b7e4
Create Date: 2026-11-17 03:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e7b2c4d91f05"
down_revision: Union[str, Sequence[str], None] = "d1f3a9c2b7e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feed_cursors",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("next_block", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_feed_cursors")),
    )
    op.drop_column("watched_wallets", "hwm_ts")


def downgrade() -> None:
    op.add_column(
        "watched_wallets", sa.Column("hwm_ts", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_table("feed_cursors")
