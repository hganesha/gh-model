"""0001_init"""
import sqlalchemy as sa
from alembic import op

revision = "0001_init"
down_revision = None


def upgrade() -> None:
    op.create_table("customers", sa.Column("id", sa.Integer, primary_key=True), sa.Column("email", sa.String, unique=True))
