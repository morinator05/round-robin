"""The daily job: flag missed duties and choose the potential drivers for today and this week."""

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.models import Duty
from app.services import duties

log = logging.getLogger(__name__)

# Arbitrary, app-wide id for the Postgres advisory lock that serializes ticks.
DAILY_TICK_LOCK_ID = 4004


async def daily_tick(session: AsyncSession, now: datetime) -> None:
    """Strike overdue duties, then make sure today's and this week's trips have potential drivers.

    Safe to run any number of times; it also commits. Runs on a schedule, on startup,
    from the Flursprecher page, and from the dashboard if today's trips are missing.
    """
    # Serialize ticks (scheduler, startup and page loads may race).
    await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": DAILY_TICK_LOCK_ID})
    missed = await duties.process_overdue(session, now)
    await duties.materialize(session, Duty.kitchen, now.date())
    await duties.materialize(session, Duty.trash, now.date())
    await session.commit()
    if missed:
        log.info("Marked %d potential drivers as missed", len(missed))


async def scheduled_tick() -> None:
    """`daily_tick` with its own session, for the scheduler (no request around it)."""
    async with SessionLocal() as session:
        await daily_tick(session, settings.now())
