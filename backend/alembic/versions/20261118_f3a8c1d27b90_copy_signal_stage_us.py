"""copy_signals.stage_us: per-stage monotonic latency from fill receipt

Revision ID: f3a8c1d27b90
Revises: e7b2c4d91f05
Create Date: 2026-11-18 04:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f3a8c1d27b90"
down_revision: Union[str, Sequence[str], None] = "e7b2c4d91f05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "copy_signals",
        sa.Column("stage_us", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("copy_signals", "stage_us")
