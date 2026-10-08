from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

_kw: dict = {"pool_pre_ping": True}          # survive a restarted/dropped database connection
if settings.database_url.startswith("postgresql"):
    _kw.update(pool_size=10, max_overflow=20)  # many clients may long-poll at once
engine = create_async_engine(settings.database_url, **_kw)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with SessionLocal() as session:
        yield session
