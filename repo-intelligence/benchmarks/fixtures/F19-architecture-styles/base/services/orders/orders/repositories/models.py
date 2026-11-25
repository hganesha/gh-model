from sqlalchemy import ForeignKey, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

engine = create_engine("postgresql://orders-db/orders")
SessionLocal = sessionmaker(engine)


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column()
    total: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(default="placed")


class OrderSummary(Base):
    """The read model: one row per customer, kept up to date from order events."""

    __tablename__ = "order_summaries"

    customer_id: Mapped[int] = mapped_column(primary_key=True)
    orders: Mapped[int] = mapped_column()
    spent: Mapped[int] = mapped_column()
