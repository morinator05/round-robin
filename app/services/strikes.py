"""Strikes, and the penalty trash week they turn into."""

from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AwayPeriod, Duty, PotentialDriver, Status, Strike, Trip, User
from app.services import carpool


async def for_potential_driver(session: AsyncSession, potential_driver_id: int) -> list[Strike]:
    """All strikes given for one duty, active or not."""
    return list(
        await session.scalars(
            select(Strike).where(Strike.potential_driver_id == potential_driver_id)
        )
    )


# Strikes that still count: neither revoked nor used up by a penalty week (in SQL, like
# `Strike.is_active`).
ACTIVE = and_(Strike.revoked_at.is_(None), Strike.redeemed_by_trip_id.is_(None))


async def active_strikes(session: AsyncSession, user_id: int) -> list[Strike]:
    """The user's strikes that still count, oldest first."""
    return list(
        await session.scalars(
            select(Strike)
            .where(Strike.user_id == user_id, ACTIVE)
            .order_by(Strike.created_at, Strike.id)
        )
    )


async def active_counts(session: AsyncSession) -> dict[int, int]:
    """Number of active strikes per user id, for everyone at once (users without any are
    missing)."""
    rows = await session.execute(
        select(Strike.user_id, func.count()).where(ACTIVE).group_by(Strike.user_id)
    )
    return dict(rows.all())


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
    """Take a strike back (by the floor speaker, or because its duty was done after all).

    If it was already used up by a penalty week that hasn't been served yet, that week is
    cancelled: its other strikes count again, and a new penalty week follows if there are
    still enough of them.
    """
    if strike.revoked_at is not None:
        return
    strike.revoked_at = now
    await session.flush()
    if strike.redeemed_by_trip_id is not None:
        trip = await session.get(Trip, strike.redeemed_by_trip_id)
        if trip is not None and await _penalty_open(session, trip):
            await cancel_penalty(session, trip)
            await check_threshold(session, strike.user_id, now.date())


async def _penalty_open(session: AsyncSession, trip: Trip) -> bool:
    """Whether nobody has served or missed the penalty `trip` yet."""
    statuses = await session.scalars(
        select(PotentialDriver.status).where(PotentialDriver.trip_id == trip.id)
    )
    return all(s is Status.pending for s in statuses)


async def cancel_penalty(session: AsyncSession, trip: Trip) -> None:
    """Delete an open penalty week; the strikes it used up count again."""
    redeemed = await session.scalars(select(Strike).where(Strike.redeemed_by_trip_id == trip.id))
    for s in redeemed:
        s.redeemed_by_trip_id = None
    await session.delete(trip)
    await session.flush()


# How far ahead a penalty week is looked for. Away periods are limited to a year ahead
# (`users.MAX_AWAY_DAYS_AHEAD`), so this is only a safety net against an endless search.
MAX_PENALTY_WEEKS_AHEAD = 104


async def next_free_penalty_week(
    session: AsyncSession, user_id: int, today: date, exclude_trip_id: int | None = None
) -> date:
    """First week after the current one without a trash trip in which the user rides.

    `exclude_trip_id`: a penalty trip that is being moved, so its own week counts as free.
    """
    taken = set(
        await session.scalars(
            select(Trip.start_date).where(
                Trip.duty == Duty.trash,
                Trip.start_date > today,
                Trip.id != (exclude_trip_id or 0),
            )
        )
    )
    away = [
        (a.start_date, a.end_date)
        for a in await session.scalars(select(AwayPeriod).where(AwayPeriod.user_id == user_id))
    ]
    member = carpool.Member(id=user_id, room="", away=tuple(away))
    first = carpool.week_start(today) + timedelta(days=7)
    weeks = [first + timedelta(days=7 * i) for i in range(MAX_PENALTY_WEEKS_AHEAD)]
    free = [w for w in weeks if w not in taken]
    return next((w for w in free if member.rides(w, w + timedelta(days=6))), free[0])


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
    # A paused resident gets their penalty week once they are active again (`set_active`).
    if user is None or not user.active:
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
