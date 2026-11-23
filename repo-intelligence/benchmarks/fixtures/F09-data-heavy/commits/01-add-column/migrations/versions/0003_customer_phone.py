"""0003_customer_phone"""
import sqlalchemy as sa
from alembic import op

revision = "0003_customer_phone"
down_revision = '0002_orders'


def upgrade() -> None:
    op.add_column("customers", sa.Column("phone", sa.String, nullable=True))
