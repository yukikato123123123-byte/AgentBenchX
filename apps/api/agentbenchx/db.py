from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import Settings


def database(settings: Settings):
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
    )
    return engine, sessionmaker(engine, expire_on_commit=False)
