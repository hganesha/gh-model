"""0005_rename_total"""
import sqlalchemy as sa
from alembic import op

revision = "0005_rename_total"
down_revision = '0004_shipments'


def upgrade() -> None:
    op.alter_column("orders", "total_cents", new_column_name="amount_cents")
