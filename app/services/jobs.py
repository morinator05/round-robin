"""The daily job: flag missed duties and choose the potential drivers for today and this week."""

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import Lock, SessionLocal, advisory_lock
from app.models import Duty, Trip
from app.services import duties

log = logging.getLogger(__name__)


async def daily_tick(session: AsyncSession, now: datetime) -> None:
    """Strike overdue duties, then make sure today's and this week's trips have potential drivers.

    A day's (week's) trip is only chosen from the deadline hour on (`duties.choose_at`).
    Safe to run any number of times; it also commits.
    """
    await advisory_lock(session, Lock.trips)
    missed = await duties.process_overdue(session, now)
    await duties.fill_current(session, now)
    await session.commit()
    if missed:
        log.info("Marked %d potential drivers as missed", len(missed))


async def current_trips(session: AsyncSession, now: datetime) -> dict[Duty, Trip | None]:
    """Each duty's `duties.current_trip`; runs the daily job first if one is missing (e.g.
    the server was down at the deadline hour)."""
    trips = {duty: await duties.current_trip(session, duty, now) for duty in Duty}
    if None in trips.values():
        await daily_tick(session, now)
        trips = {duty: await duties.current_trip(session, duty, now) for duty in Duty}
    return trips


async def scheduled_tick() -> None:
    """`daily_tick` with its own session, for the scheduler (no request around it)."""
    async with SessionLocal() as session:
        await daily_tick(session, settings.now())
