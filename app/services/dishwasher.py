"""The dishwasher list: whoever empties the dishwasher marks it, as often as they do; the
month's top ones become dishwasher heroes and get the next month off kitchen duty.

The heroes are never stored but worked out from the previous month's emptyings. Past months
can't be changed (`delete_clear`, `dispute_clear`), so a month's heroes stay the same while
it runs.

Entries are valid by default. Bursts (`suspicious`) are for the floor speaker to check: they
dispute an entry (it no longer counts) or confirm it as valid.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import DishwasherClear, User
from app.services.errors import DishwasherError


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
    """`day`'s month as a time range in the configured time zone: from its first midnight up
    to (not including) the next month's."""
    return (
        datetime.combine(month_start(day), time(0), settings.tz),
        datetime.combine(next_month(day), time(0), settings.tz),
    )


async def last_clear(session: AsyncSession) -> DishwasherClear | None:
    """The latest emptying that counts (not disputed), with who did it."""
    return await session.scalar(
        select(DishwasherClear)
        .where(DishwasherClear.disputed_at.is_(None))
        .order_by(DishwasherClear.cleared_at.desc(), DishwasherClear.id.desc())
        .options(selectinload(DishwasherClear.user))
        .limit(1)
    )


async def mark_cleared(session: AsyncSession, user: User, now: datetime) -> DishwasherClear:
    """`user` emptied the dishwasher at `now`. Any time, as often as they like.

    Raises:
        DishwasherError: if `user` is paused.
    """
    if not user.active:
        raise DishwasherError("Inactive residents can't mark the dishwasher")
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
    first. Disputed emptyings don't count, moved-out residents' count for nobody."""
    start, end = month_window(day)
    rows = await session.execute(
        select(
            DishwasherClear.user_id,
            func.count().label("n"),
            func.max(DishwasherClear.cleared_at).label("reached"),
        )
        .where(
            DishwasherClear.user_id.is_not(None),
            DishwasherClear.disputed_at.is_(None),
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


@dataclass(frozen=True)
class Summary:
    """One resident's part of the dishwasher list this month."""

    # Their emptyings this month.
    mine: int
    # This month's heroes (user ids), the top of `heroes_from`.
    heroes: list[int]
    heroes_from: date

    def is_hero(self, user_id: int) -> bool:
        return user_id in self.heroes


async def summary_for(session: AsyncSession, user: User, today: date) -> Summary:
    """`user`'s emptyings in `today`'s month, and the month's heroes."""
    mine = next((s.count for s in await standings(session, today) if s.user_id == user.id), 0)
    return Summary(mine, await heroes(session, today), previous_month(today))


async def clears_in_month(session: AsyncSession, day: date) -> list[DishwasherClear]:
    """The emptyings of `day`'s month, also disputed ones, newest first, with who did them
    and who decided about them."""
    start, end = month_window(day)
    return list(
        await session.scalars(
            select(DishwasherClear)
            .where(DishwasherClear.cleared_at >= start, DishwasherClear.cleared_at < end)
            .order_by(DishwasherClear.cleared_at.desc(), DishwasherClear.id.desc())
            .options(
                selectinload(DishwasherClear.user),
                selectinload(DishwasherClear.disputed_by),
                selectinload(DishwasherClear.confirmed_by),
            )
        )
    )


def suspicious(clears: Iterable[DishwasherClear]) -> set[int]:
    """The ids of `clears` in a burst: `settings.dishwasher_suspicious_count` or more of
    them less than `settings.dishwasher_suspicious_minutes` apart, first to last, whoever
    made them. Disputed ones don't count, so they're never in one."""
    n = max(settings.dishwasher_suspicious_count, 1)
    window = timedelta(minutes=settings.dishwasher_suspicious_minutes)
    counted = sorted(
        (c for c in clears if c.disputed_at is None), key=lambda c: (c.cleared_at, c.id)
    )
    found = set()
    for i in range(len(counted) - n + 1):
        run = counted[i : i + n]
        if run[-1].cleared_at - run[0].cleared_at < window:
            found.update(c.id for c in run)
    return found


def needs_review(clears: list[DishwasherClear]) -> list[DishwasherClear]:
    """The suspicious ones of `clears` that the floor speaker hasn't confirmed yet, in the
    same order."""
    found = suspicious(clears)
    return [c for c in clears if c.id in found and c.confirmed_at is None]


def _check_decision(clear: DishwasherClear, actor: User, now: datetime, do: str, done: str):
    """The floor speaker decides about entries, only about this month's: earlier months are
    final, so their heroes don't change while their month runs.

    Raises:
        DishwasherError: "Only the floor speaker can `do`" / "... can be `done`".
    """
    if not actor.is_admin:
        raise DishwasherError(f"Only the floor speaker can {do}")
    start, _ = month_window(now.date())
    if clear.cleared_at < start:
        raise DishwasherError(
            f"Only this month's entries can be {done}: earlier months decided the heroes"
        )


async def delete_clear(
    session: AsyncSession, clear: DishwasherClear, actor: User, now: datetime
) -> None:
    """Admin only: remove a wrongly marked emptying of the current month.

    Raises:
        DishwasherError: if `actor` isn't the floor speaker or the entry is from an
            earlier month.
    """
    _check_decision(clear, actor, now, "delete dishwasher entries", "deleted")
    await session.delete(clear)
    await session.flush()


async def dispute_clear(
    session: AsyncSession, clear: DishwasherClear, actor: User, now: datetime
) -> None:
    """Admin only: `clear` wasn't a real emptying. It stays in the list, with who disputed
    it, but no longer counts: not toward the heroes, not toward a burst. No strike (the
    floor speaker can give one). Also for one confirmed before.

    Raises:
        DishwasherError: if `actor` isn't the floor speaker, the entry is from an earlier
            month or already disputed.
    """
    _check_decision(clear, actor, now, "dispute dishwasher entries", "disputed")
    if clear.disputed_at is not None:
        raise DishwasherError("This entry is already disputed")
    clear.disputed_at, clear.disputed_by_id = now, actor.id
    clear.confirmed_at = clear.confirmed_by_id = None
    await session.flush()


async def confirm_clear(
    session: AsyncSession, clear: DishwasherClear, actor: User, now: datetime
) -> None:
    """Admin only: the floor speaker checked `clear` and it's valid. It no longer needs
    their attention, and counts again if it was disputed.

    Raises:
        DishwasherError: if `actor` isn't the floor speaker, the entry is from an earlier
            month or already confirmed.
    """
    _check_decision(clear, actor, now, "mark dishwasher entries valid", "marked valid")
    if clear.confirmed_at is not None:
        raise DishwasherError("This entry is already marked valid")
    clear.confirmed_at, clear.confirmed_by_id = now, actor.id
    clear.disputed_at = clear.disputed_by_id = None
    await session.flush()


def can_undo_clear(clear: DishwasherClear, user: User, now: datetime) -> bool:
    """Whether `user` may take back `clear` now.

    Only their own, within `settings.undo_minutes`, not once the floor speaker decided
    about it, and not once a new month has begun (the old one may have decided the heroes).
    """
    start, _ = month_window(now.date())
    return (
        clear.user_id == user.id
        and now - clear.cleared_at <= timedelta(minutes=settings.undo_minutes)
        and clear.cleared_at >= start
        and clear.disputed_at is None
        and clear.confirmed_at is None
    )


async def undo_clear(
    session: AsyncSession, clear: DishwasherClear, user: User, now: datetime
) -> None:
    """Take back a mis-tapped emptying: it no longer counts.

    Raises:
        DishwasherError: if `user` may not (any more), see `can_undo_clear`.
    """
    if not can_undo_clear(clear, user, now):
        raise DishwasherError(
            f"An emptying can only be taken back within {settings.undo_minutes} minutes, "
            "by whoever marked it"
        )
    await session.delete(clear)
    await session.flush()
