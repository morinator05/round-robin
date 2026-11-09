"""The dishwasher list: whoever empties the dishwasher marks it; the month's top ones become
dishwasher heroes and get the next month off kitchen duty.

Like the fairness scores, the heroes are never stored: they are worked out from the previous
month's emptyings when needed. Past months can't be changed (`delete_clear`), so a month's
heroes stay the same while it runs. `duties.load_members` hands the heroes to the kitchen
carpool as an away period over their month, so they ride on no kitchen trip then: they
aren't chosen and owe no share for it.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import DishwasherClear, User


class DishwasherError(Exception):
    """An action on the dishwasher list isn't allowed; the message is shown as is."""


# Arbitrary, app-wide id for the Postgres advisory lock that serializes new emptyings.
DISHWASHER_LOCK_ID = 4005


def month_start(day: date) -> date:
    """The first day of `day`'s month."""
    return day.replace(day=1)


def next_month(day: date) -> date:
    """The first day of the month after `day`'s."""
    return (month_start(day) + timedelta(days=32)).replace(day=1)


def previous_month(day: date) -> date:
    """The first day of the month before `day`'s."""
    return (month_start(day) - timedelta(days=1)).replace(day=1)


def month_bounds(day: date) -> tuple[date, date]:
    """First and last day of `day`'s month."""
    return month_start(day), next_month(day) - timedelta(days=1)


def month_window(day: date) -> tuple[datetime, datetime]:
    """`day`'s month as a time range: from its first midnight up to (not including) the next
    month's, in the configured time zone."""
    return (
        datetime.combine(month_start(day), time(0), settings.tz),
        datetime.combine(next_month(day), time(0), settings.tz),
    )


def cooldown() -> timedelta:
    """Time after an emptying before the next one can be marked."""
    return timedelta(minutes=settings.dishwasher_cooldown_minutes)


def clock(moment: datetime) -> str:
    """The time of day like "14:32", in the configured time zone."""
    return f"{moment.astimezone(settings.tz):%H:%M}"


async def last_clear(session: AsyncSession) -> DishwasherClear | None:
    """The latest emptying, with who did it."""
    return await session.scalar(
        select(DishwasherClear)
        .order_by(DishwasherClear.cleared_at.desc(), DishwasherClear.id.desc())
        .options(selectinload(DishwasherClear.user))
        .limit(1)
    )


def next_allowed(last: DishwasherClear | None) -> datetime | None:
    """From when the next emptying can be marked; None if any time."""
    return last.cleared_at + cooldown() if last else None


async def mark_cleared(session: AsyncSession, user: User, now: datetime) -> DishwasherClear:
    """`user` emptied the dishwasher at `now`.

    Only one emptying per cooldown counts, whoever marks it: after one, nobody can mark the
    next for `settings.dishwasher_cooldown_minutes`.

    Raises:
        DishwasherError: if `user` is paused or the last emptying is too recent.
    """
    if not user.active:
        raise DishwasherError("Inactive residents can't mark the dishwasher")
    # Two clicks at the same moment: the second one waits and then sees the first.
    await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": DISHWASHER_LOCK_ID})
    last = await last_clear(session)
    allowed = next_allowed(last)
    if allowed is not None and now < allowed:
        who = (
            "you"
            if last.user_id == user.id
            else (last.user.display_name if last.user else "a former resident")
        )
        raise DishwasherError(
            f"The dishwasher was already emptied by {who} at {clock(last.cleared_at)}; "
            f"the next one can be marked from {clock(allowed)}"
        )
    clear = DishwasherClear(user_id=user.id, cleared_at=now)
    session.add(clear)
    await session.flush()
    return clear


@dataclass(frozen=True)
class Standing:
    """One resident's emptyings in a month."""

    user_id: int
    count: int
    # When they reached `count` (their last emptying that month); earlier wins a tie.
    reached_at: datetime


async def standings(session: AsyncSession, day: date) -> list[Standing]:
    """Everyone with emptyings in `day`'s month, the most first; on a tie whoever got there
    first. Moved-out residents' emptyings count for nobody."""
    start, end = month_window(day)
    rows = await session.execute(
        select(
            DishwasherClear.user_id,
            func.count().label("n"),
            func.max(DishwasherClear.cleared_at).label("reached"),
        )
        .where(
            DishwasherClear.user_id.is_not(None),
            DishwasherClear.cleared_at >= start,
            DishwasherClear.cleared_at < end,
        )
        .group_by(DishwasherClear.user_id)
        .order_by(
            func.count().desc(), func.max(DishwasherClear.cleared_at), DishwasherClear.user_id
        )
    )
    return [Standing(uid, n, reached) for uid, n, reached in rows]


async def heroes(session: AsyncSession, today: date) -> list[int]:
    """The dishwasher heroes of `today`'s month: the top of the month before (user ids)."""
    top = await standings(session, previous_month(today))
    return [s.user_id for s in top[: settings.dishwasher_heroes]]


async def clears_in_month(session: AsyncSession, day: date) -> list[DishwasherClear]:
    """The emptyings of `day`'s month, newest first, with who did them."""
    start, end = month_window(day)
    return list(
        await session.scalars(
            select(DishwasherClear)
            .where(DishwasherClear.cleared_at >= start, DishwasherClear.cleared_at < end)
            .order_by(DishwasherClear.cleared_at.desc(), DishwasherClear.id.desc())
            .options(selectinload(DishwasherClear.user))
        )
    )


async def delete_clear(
    session: AsyncSession, clear: DishwasherClear, actor: User, now: datetime
) -> None:
    """Admin only: remove a wrongly marked emptying of the current month.

    Earlier months are final, so their heroes don't change while their month runs.

    Raises:
        DishwasherError: if `actor` isn't the floor speaker or the entry is from an
            earlier month.
    """
    if not actor.is_admin:
        raise DishwasherError("Only the floor speaker can delete dishwasher entries")
    start, _ = month_window(now.date())
    if clear.cleared_at < start:
        raise DishwasherError(
            "Only this month's entries can be deleted: earlier months decided the heroes"
        )
    await session.delete(clear)
    await session.flush()


# How long whoever marked an emptying can take it back (a mis-tap), see `undo_clear`.
UNDO_MINUTES = 10


def can_undo_clear(clear: DishwasherClear, user: User, now: datetime) -> bool:
    """Whether `user` may take back `clear` now: only their own, within `UNDO_MINUTES`, and
    not once a new month has begun (the old one may have decided the heroes)."""
    start, _ = month_window(now.date())
    return (
        clear.user_id == user.id
        and now - clear.cleared_at <= timedelta(minutes=UNDO_MINUTES)
        and clear.cleared_at >= start
    )


async def undo_clear(
    session: AsyncSession, clear: DishwasherClear, user: User, now: datetime
) -> None:
    """Take back a mis-tapped emptying: it no longer counts, and the next one can be marked
    right away.

    Raises:
        DishwasherError: if `user` may not (any more), see `can_undo_clear`.
    """
    if not can_undo_clear(clear, user, now):
        raise DishwasherError(
            f"An emptying can only be taken back within {UNDO_MINUTES} minutes, by whoever "
            "marked it"
        )
    await session.delete(clear)
    await session.flush()
