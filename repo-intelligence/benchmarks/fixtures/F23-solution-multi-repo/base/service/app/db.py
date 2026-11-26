import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

engine = create_engine(os.environ["DATABASE_URL"])
Session = sessionmaker(engine)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info")


class Base(DeclarativeBase):
    pass
