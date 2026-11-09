"""Database engine, sessions and locks."""

import enum
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url)
# expire_on_commit=False: objects stay readable after commit, without another query
# (an implicit reload would fail in async code).
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """One session per request; closed (and rolled back if not committed) afterwards."""
    async with SessionLocal() as session:
        yield session


class Lock(enum.IntEnum):
    """The app's Postgres advisory locks (arbitrary, unique ids)."""

    # Creating and reassigning trips: the daily job and admin actions can't both create
    # the same trip, or choose a penalty week someone else is choosing.
    trips = 4004
    # Marking the dishwasher: two clicks at once, the second waits and sees the first.
    dishwasher = 4005


async def advisory_lock(session: AsyncSession, lock: Lock) -> None:
    """Wait for `lock` and hold it until the transaction ends. Re-entrant within it."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": int(lock)})
