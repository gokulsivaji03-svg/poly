"""accounts.ledger_version: a trigger-maintained version of the account, its lots and orders

Revision ID: c71e5b0a9d24
Revises: a9d4e2f61c37
Create Date: 2026-11-18 10:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c71e5b0a9d24"
down_revision: Union[str, Sequence[str], None] = "a9d4e2f61c37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATEMENTS = (
    """CREATE OR REPLACE FUNCTION accounts_bump_ledger_version() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        NEW.ledger_version := OLD.ledger_version + 1;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER accounts_ledger_version BEFORE UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION accounts_bump_ledger_version()""",
    """CREATE OR REPLACE FUNCTION bump_account_ledger_version() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        EXECUTE format(
            'UPDATE %I.accounts SET ledger_version = ledger_version + 1 WHERE id = $1',
            TG_TABLE_SCHEMA
        ) USING CASE WHEN TG_OP = 'DELETE' THEN OLD.account_id ELSE NEW.account_id END;
        RETURN NULL;
    END $$""",
    """CREATE CONSTRAINT TRIGGER lots_ledger_version AFTER INSERT OR UPDATE OR DELETE
    ON lots DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION bump_account_ledger_version()""",
    """CREATE CONSTRAINT TRIGGER orders_ledger_version AFTER INSERT OR UPDATE OR DELETE
    ON orders DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION bump_account_ledger_version()""",
)


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("ledger_version", sa.BigInteger(), server_default="0", nullable=False),
    )
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER orders_ledger_version ON orders")
    op.execute("DROP TRIGGER lots_ledger_version ON lots")
    op.execute("DROP TRIGGER accounts_ledger_version ON accounts")
    op.execute("DROP FUNCTION bump_account_ledger_version()")
    op.execute("DROP FUNCTION accounts_bump_ledger_version()")
    op.drop_column("accounts", "ledger_version")
