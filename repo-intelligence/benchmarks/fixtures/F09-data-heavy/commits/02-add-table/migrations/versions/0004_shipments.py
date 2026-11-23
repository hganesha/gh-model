"""0004_shipments"""
import sqlalchemy as sa
from alembic import op

revision = "0004_shipments"
down_revision = '0003_customer_phone'


def upgrade() -> None:
    op.create_table("shipments", sa.Column("id", sa.Integer, primary_key=True), sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id")))
