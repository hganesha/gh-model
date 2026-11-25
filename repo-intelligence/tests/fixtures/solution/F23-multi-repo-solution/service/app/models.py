from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column()
    total: Mapped[float] = mapped_column()
