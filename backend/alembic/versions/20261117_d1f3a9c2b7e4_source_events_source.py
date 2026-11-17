"""source_events.source: where an activity row was first seen (api | chain)

Revision ID: d1f3a9c2b7e4
Revises: ce102b742df7
Create Date: 2026-11-17 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d1f3a9c2b7e4"
down_revision: Union[str, Sequence[str], None] = "ce102b742df7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_events",
        sa.Column("source", sa.String(length=8), server_default="api", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("source_events", "source")
