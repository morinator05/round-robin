"""Strikes, and the penalty trash week they turn into."""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AwayPeriod, Duty, PotentialDriver, Strike, Trip, User
from app.services import carpool


async def for_potential_driver(session: AsyncSession, potential_driver_id: int) -> list[Strike]:
    """All strikes given for one duty, active or not."""
    return list(
        await session.scalars(
            select(Strike).where(Strike.potential_driver_id == potential_driver_id)
        )
    )


async def active_strikes(session: AsyncSession, user_id: int) -> list[Strike]:
    """The user's strikes that still count, oldest first."""
    return list(
        await session.scalars(
            select(Strike)
            .where(
                Strike.user_id == user_id,
                Strike.revoked_at.is_(None),
                Strike.redeemed_by_trip_id.is_(None),
            )
            .order_by(Strike.created_at, Strike.id)
        )
    )


async def issue(
    session: AsyncSession,
    *,
    user_id: int,
    reason: str,
    today: date,
    auto: bool = False,
    potential_driver_id: int | None = None,
    issued_by_id: int | None = None,
) -> Strike:
    """Give a strike; if it is the user's `strike_threshold`-th, a penalty week follows.

    Args:
        today: Used to find the penalty week (the first free one after this week).
        auto: True when the daily job gives it for a missed duty.
        potential_driver_id: The duty it is for, so doing that duty later can revoke it.
        issued_by_id: The floor speaker who gave it, if given by hand.
    """
    strike = Strike(
        user_id=user_id,
        reason=reason,
        auto=auto,
        potential_driver_id=potential_driver_id,
        issued_by_id=issued_by_id,
    )
    session.add(strike)
    await session.flush()
    await check_threshold(session, user_id, today)
    return strike


async def revoke(session: AsyncSession, strike: Strike, now: datetime) -> None:
    """The floor speaker takes a strike back."""
    strike.revoked_at = now
    await session.flush()


async def next_free_penalty_week(session: AsyncSession, user_id: int, today: date) -> date:
    """First week after the current one without a trash trip in which the user rides."""
    taken = set(
        await session.scalars(
            select(Trip.start_date).where(Trip.duty == Duty.trash, Trip.start_date > today)
        )
    )
    away = [
        (a.start_date, a.end_date)
        for a in await session.scalars(select(AwayPeriod).where(AwayPeriod.user_id == user_id))
    ]
    member = carpool.Member(id=user_id, room="", away=tuple(away))
    week = carpool.week_start(today) + timedelta(days=7)
    while week in taken or not member.rides(week, week + timedelta(days=6)):
        week += timedelta(days=7)
    return week


async def check_threshold(session: AsyncSession, user_id: int, today: date) -> Trip | None:
    """Turn `strike_threshold` active strikes into a penalty trash week.

    The penalty trip replaces the regular trash duty of that week; the user is its only
    potential driver. The oldest strikes are marked as paid off by it, so they no longer
    count.

    Returns:
        The penalty trip, or None if the user doesn't have enough strikes.
    """
    active = await active_strikes(session, user_id)
    if len(active) < settings.strike_threshold:
        return None
    user = await session.get(User, user_id)
    if user is None:
        return None

    week = await next_free_penalty_week(session, user_id, today)
    trip = Trip(
        duty=Duty.trash,
        start_date=week,
        end_date=week + timedelta(days=6),
        is_penalty=True,
        potential_drivers=[PotentialDriver(user_id=user_id)],
    )
    session.add(trip)
    await session.flush()
    for s in active[: settings.strike_threshold]:
        s.redeemed_by_trip_id = trip.id
    await session.flush()
    return trip
