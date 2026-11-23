from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str | None] = mapped_column(default=None)
