"""0002_orders"""
import sqlalchemy as sa
from alembic import op

revision = "0002_orders"
down_revision = '0001_init'


def upgrade() -> None:
    op.create_table("orders", sa.Column("id", sa.Integer, primary_key=True), sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id")), sa.Column("total_cents", sa.Integer))
    op.create_table("order_items", sa.Column("id", sa.Integer, primary_key=True), sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id")), sa.Column("sku", sa.String))
