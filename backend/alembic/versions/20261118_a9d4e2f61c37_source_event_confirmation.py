"""source_events.confirmation: lifecycle of fills copied from pending transactions

Revision ID: a9d4e2f61c37
Revises: f3a8c1d27b90
Create Date: 2026-11-18 05:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a9d4e2f61c37"
down_revision: Union[str, Sequence[str], None] = "f3a8c1d27b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("source_events", sa.Column("confirmation", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("source_events", "confirmation")
