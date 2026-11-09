"""Database side of the duty carpools: creating trips, marking who drove, missed duties.

Trips are only created ("materialized") for the current day and week. Future ones are just
simulated for the preview, so they always reflect the latest balances and away periods.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import Lock, advisory_lock
from app.formatting import long_date
from app.models import MAX_ID, Duty, PotentialDriver, Rider, Status, Trip, User
from app.services import carpool, dishwasher, strikes
from app.services.carpool import Balance, Member
from app.services.errors import DutyError

# How long a "Done" can be taken back (a mis-tap), see `undo_driver`.
UNDO_MINUTES = 10


def potential_drivers_per_trip(duty: Duty) -> int:
    """How many potential drivers a trip gets. Only one of them has to actually drive."""
    if duty is Duty.kitchen:
        return settings.kitchen_people_per_day
    return settings.trash_people_per_week


def trip_bounds(duty: Duty, day: date) -> tuple[date, date]:
    """First and last day of the trip containing `day`: the day itself, or its Mon-Sun week."""
    if duty is Duty.kitchen:
        return day, day
    start = carpool.week_start(day)
    return start, start + timedelta(days=6)


def deadline(trip: Trip) -> datetime:
    """Until when the trip can be marked driven: the deadline hour on the day after it ended.

    After that it counts as missed.
    """
    day = trip.end_date + timedelta(days=1)
    return datetime.combine(day, time(settings.deadline_hour), settings.tz)


def choose_at(duty: Duty, day: date) -> datetime:
    """When the potential drivers of the trip containing `day` are chosen.

    The deadline hour of the trip's first day, right after the previous trip's deadline,
    so only one trip is ever open and the preview can be exact for each outcome of it.
    """
    start, _ = trip_bounds(duty, day)
    return datetime.combine(start, time(settings.deadline_hour), settings.tz)


async def current_trip(session: AsyncSession, duty: Duty, now: datetime) -> Trip | None:
    """The trip of `duty` that is on at `now`, shown on the dashboard.

    Today's (this week's) trip once it's chosen (`choose_at`); before that the previous
    one, which can still be marked until its deadline. None if it doesn't exist.
    """
    today = now.date()
    if now >= choose_at(duty, today):
        return await get_trip(session, duty, today)
    start, _ = trip_bounds(duty, today)
    return await get_trip(session, duty, start - timedelta(days=1))


async def load_members(session: AsyncSession, duty: Duty, today: date) -> list[Member]:
    """All active users (the carpool's members of `duty`) with their away periods.

    In the kitchen carpool, `today`'s month counts as an away period of its dishwasher
    heroes: they get it off kitchen duty, without owing a share for it.
    """
    users = await session.scalars(
        select(User)
        .where(User.active.is_(True))
        .options(selectinload(User.away_periods))
        .execution_options(populate_existing=True)
    )
    heroes = set(await dishwasher.heroes(session, today)) if duty is Duty.kitchen else set()
    hero_month = (dishwasher.month_bounds(today),)
    return [
        Member(
            id=u.id,
            room=u.room_number,
            away=tuple((a.start_date, a.end_date) for a in u.away_periods)
            + (hero_month if u.id in heroes else ()),
        )
        for u in users
    ]


def was_driven(trip: Trip) -> bool:
    """Whether `trip` counts for fairness: a regular trip that someone drove."""
    return not trip.is_penalty and trip.driver is not None


def reserved_share(trip: Trip, pd: PotentialDriver) -> Fraction:
    """`pd`'s part of a trip nobody has driven yet, for choosing (`Balance.pending_share`)."""
    if trip.is_penalty or pd.status is not Status.pending or trip.driver is not None:
        return Fraction(0)
    return Fraction(1, sum(x.status is Status.pending for x in trip.potential_drivers))


async def load_balances(session: AsyncSession, duty: Duty) -> dict[int, Balance]:
    """Every user's balance in the carpool of `duty`, computed from all trips so far.

    Only driven trips count (`was_driven`), so the scores sum to 0, apart from the scores
    of residents who moved out.
    """
    balances: dict[int, Balance] = {}
    trips = await session.scalars(
        select(Trip)
        .where(Trip.duty == duty, Trip.is_penalty.is_(False))
        .options(selectinload(Trip.riders), selectinload(Trip.potential_drivers))
        .execution_options(populate_existing=True)
    )
    for trip in trips:
        if was_driven(trip):
            carpool.charge_riders(balances, [r.user_id for r in trip.riders])
        for pd in trip.potential_drivers:
            if pd.user_id is None:
                continue
            if pd.status is Status.drove:
                carpool.credit_driver(balances, pd.user_id, trip.start_date)
            carpool.reserve_share(balances, pd.user_id, reserved_share(trip, pd))
    return balances


async def points(session: AsyncSession, users: list[User]) -> dict[Duty, dict[int, int]]:
    """Each user's score per duty as whole points (`carpool.display_points`).

    Among equal scores, whoever is chosen sooner shows fewer points.
    """
    ids = [u.id for u in users]
    rooms = {u.id: u.room_number for u in users}
    result = {}
    for duty in Duty:
        balances = await load_balances(session, duty)
        queue = carpool.choose_potential_drivers(balances, ids, len(ids), rooms)
        scores = {uid: balances[uid].score if uid in balances else Fraction(0) for uid in ids}
        result[duty] = carpool.display_points(scores, queue)
    return result


async def get_trip(session: AsyncSession, duty: Duty, day: date) -> Trip | None:
    """The trip containing `day`, with its potential drivers loaded, or None if not created yet."""
    start, _ = trip_bounds(duty, day)
    return await session.scalar(
        select(Trip)
        .where(Trip.duty == duty, Trip.start_date == start)
        .options(selectinload(Trip.potential_drivers).selectinload(PotentialDriver.user))
    )


async def materialize(session: AsyncSession, duty: Duty, day: date) -> Trip:
    """Create the trip containing `day` and choose its potential drivers.

    An existing trip that has fewer potential drivers than needed (e.g. it was created
    before enough residents existed) is topped up, unless someone already drove it.
    """
    trip = await get_trip(session, duty, day)
    missing = potential_drivers_per_trip(duty) - len(trip.potential_drivers if trip else [])
    if trip is not None and (trip.is_penalty or trip.driver is not None or missing <= 0):
        return trip
    members = await load_members(session, duty, trip.start_date if trip else day)
    if trip is None:
        start, end = trip_bounds(duty, day)
        riders = [Rider(user_id=m.id) for m in members if m.rides(start, end)]
        trip = Trip(
            duty=duty,
            start_date=start,
            end_date=end,
            is_penalty=False,
            riders=riders,
            potential_drivers=[],
        )
        session.add(trip)
    else:
        await session.refresh(trip, ["riders"])
        _add_missing_riders(trip, members)  # residents added since
    await session.flush()
    await _add_potential_drivers(session, trip, missing, {m.id: m.room for m in members})
    return await get_trip(session, duty, day)


def _add_missing_riders(trip: Trip, members: list[Member]) -> None:
    """Make every member who rides on `trip` (is available for it) one of its riders."""
    known = {r.user_id for r in trip.riders}
    for m in members:
        if m.id not in known and m.rides(trip.start_date, trip.end_date):
            trip.riders.append(Rider(user_id=m.id))


async def add_riders(session: AsyncSession, today: date) -> None:
    """Whoever is available again (back from an absence, resumed) rides on today's and
    upcoming trips that nobody has driven yet, so they owe their share of them again."""
    await advisory_lock(session, Lock.trips)
    for duty in Duty:
        members = await load_members(session, duty, today)
        trips = await session.scalars(
            select(Trip)
            .where(Trip.duty == duty, Trip.is_penalty.is_(False), Trip.end_date >= today)
            .options(selectinload(Trip.riders), selectinload(Trip.potential_drivers))
            .execution_options(populate_existing=True)
        )
        for trip in trips.all():
            if not was_driven(trip):
                _add_missing_riders(trip, members)
    await session.flush()


async def _add_potential_drivers(
    session: AsyncSession, trip: Trip, count: int, rooms: dict[int, str]
) -> None:
    """Add the `count` riders with the lowest score that aren't potential drivers yet."""
    balances = await load_balances(session, trip.duty)
    taken = {pd.user_id for pd in trip.potential_drivers}
    pool = [r.user_id for r in trip.riders if r.user_id is not None and r.user_id not in taken]
    for uid in carpool.choose_potential_drivers(balances, pool, count, rooms):
        trip.potential_drivers.append(PotentialDriver(user_id=uid))
    await session.flush()


async def fill_current(session: AsyncSession, now: datetime) -> None:
    """Make sure today's and this week's trips exist and have enough potential drivers.

    Only from their `choose_at` on. Before that the previous trip is still on, but its day
    is over: nobody new is put on it.
    """
    await advisory_lock(session, Lock.trips)
    for duty in Duty:
        if now >= choose_at(duty, now.date()):
            await materialize(session, duty, now.date())


async def reconcile(session: AsyncSession, now: datetime) -> None:
    """Take whoever no longer rides off the trips that are still open, and replace them.

    After someone is marked away, paused or deleted. They stop riding on trips nobody has
    driven yet, so they owe no share of them; moved-out residents (no user id) stay riders,
    so their share is dropped, not passed on to the others. A duty someone already has
    stays theirs while they are away (the floor speaker can excuse it); paused or moved
    out, it goes to someone else, except on a trip whose days are over: nobody new is put
    on that. Penalty weeks move to their person's next free week instead.
    """
    await advisory_lock(session, Lock.trips)
    today = now.date()
    # Per duty: dishwasher heroes ride on trash trips but not on kitchen ones.
    members = {d: {m.id: m for m in await load_members(session, d, today)} for d in Duty}
    rooms = {uid: m.room for uid, m in members[Duty.trash].items()}
    # Yesterday's trip (last week's) is still open until its deadline. Its potential
    # drivers are locked like `get_potential_driver` does, so none is removed while being
    # marked done.
    recent = select(Trip.id).where(Trip.end_date >= today - timedelta(days=1))
    await session.execute(
        select(PotentialDriver.id)
        .where(PotentialDriver.trip_id.in_(recent))
        .order_by(PotentialDriver.id)
        .with_for_update()
    )
    trips = await session.scalars(
        select(Trip)
        .where(Trip.id.in_(recent))
        .options(selectinload(Trip.riders), selectinload(Trip.potential_drivers))
        .execution_options(populate_existing=True)
    )
    for trip in trips.all():
        if deadline(trip) <= now:
            continue
        if trip.is_penalty:
            await _reconcile_penalty(session, trip, members[trip.duty], today)
        else:
            replace = trip.end_date >= today
            await _reconcile_regular(session, trip, members[trip.duty], rooms, replace)


async def _reconcile_regular(
    session: AsyncSession,
    trip: Trip,
    members: dict[int, Member],
    rooms: dict[int, str],
    replace: bool,
) -> None:
    """Take whoever no longer rides off a regular `trip`, replacing them if `replace`."""
    # Pending potential drivers who are still members keep their duty, also when away.
    keep = {
        pd.user_id
        for pd in trip.potential_drivers
        if pd.status is Status.pending and pd.user_id in members
    }

    def rides(user_id: int | None) -> bool:
        member = members.get(user_id)
        return member is not None and member.rides(trip.start_date, trip.end_date)

    riders = [r for r in trip.riders if r.user_id is None or r.user_id in keep or rides(r.user_id)]
    if not was_driven(trip) and len(riders) != len(trip.riders):
        trip.riders = riders
        await session.flush()
    gone = [
        pd
        for pd in trip.potential_drivers
        if pd.status is Status.pending and pd.user_id not in keep
    ]
    if not gone:
        return
    for pd in gone:
        trip.potential_drivers.remove(pd)
        await session.delete(pd)
    await session.flush()
    if replace:
        await _add_potential_drivers(session, trip, len(gone), rooms)


async def _reconcile_penalty(
    session: AsyncSession, trip: Trip, members: dict[int, Member], today: date
) -> None:
    """Move an open penalty week whose person no longer rides on it (see `reconcile`)."""
    pd = next(iter(trip.potential_drivers), None)
    if pd is None or pd.status is not Status.pending or pd.user_id is None:
        return
    member = members.get(pd.user_id)
    if member is None:  # paused; deleted residents' penalty weeks are gone already
        await strikes.cancel_penalty(session, trip)
        return
    if member.rides(trip.start_date, trip.end_date):
        return
    week = await strikes.next_free_penalty_week(session, pd.user_id, today, trip.id)
    trip.start_date, trip.end_date = week, week + timedelta(days=6)
    await session.flush()


async def process_overdue(session: AsyncSession, now: datetime) -> list[PotentialDriver]:
    """Mark pending potential drivers past their deadline as missed and strike them."""
    pending = (
        await session.scalars(
            select(PotentialDriver)
            .join(PotentialDriver.trip)
            .where(PotentialDriver.status == Status.pending)
            .order_by(PotentialDriver.id)
            .options(selectinload(PotentialDriver.trip))
            # Waits for a duty being marked right now (`get_potential_driver` locks it too);
            # Postgres then checks the status again, so it isn't struck after being marked.
            .with_for_update(of=PotentialDriver)
        )
    ).all()
    missed = [pd for pd in pending if deadline(pd.trip) <= now]
    for pd in missed:
        pd.status = Status.missed
        if pd.user_id is not None:
            what = f"{pd.trip.duty.label} on {long_date(pd.trip.start_date)}"
            await strikes.issue(
                session,
                user_id=pd.user_id,
                reason=f"{what} not marked as done",
                auto=True,
                potential_driver_id=pd.id,
                today=now.date(),
            )
    await session.flush()
    return missed


async def get_potential_driver(session: AsyncSession, potential_driver_id: int) -> PotentialDriver:
    """The potential driver with their trip and the trip's other potential drivers loaded.

    The trip's potential drivers are locked until the transaction ends, so two people
    marking the same trip at once (or the daily job) wait for each other.

    Raises:
        DutyError: if there is no such potential driver.
    """
    trip_id = None
    if 0 < potential_driver_id <= MAX_ID:
        trip_id = await session.scalar(
            select(PotentialDriver.trip_id).where(PotentialDriver.id == potential_driver_id)
        )
    if trip_id is None:
        raise DutyError("Duty not found")
    # Locked before they are loaded, so a request's (fresh) session reads them as they are
    # after the other transaction finished.
    await session.execute(
        select(PotentialDriver.id)
        .where(PotentialDriver.trip_id == trip_id)
        .order_by(PotentialDriver.id)
        .with_for_update()
    )
    pd = await session.scalar(
        select(PotentialDriver)
        .where(PotentialDriver.id == potential_driver_id)
        .options(
            selectinload(PotentialDriver.trip)
            .selectinload(Trip.potential_drivers)
            .selectinload(PotentialDriver.user)
        )
    )
    if pd is None:
        raise DutyError("Duty not found")
    return pd


def can_mark_as_driver(pd: PotentialDriver, actor: User, now: datetime) -> bool:
    """Whether `actor` may mark `pd` as the trip's driver now.

    Residents: only their own open duty, from its first day until the deadline.
    The floor speaker: any duty at any time, as long as nobody has driven the trip.
    """
    if pd.trip.driver is not None:
        return False
    if actor.is_admin:
        return True
    if pd.status is not Status.pending:
        return False
    if not (pd.trip.start_date <= now.date() and now < deadline(pd.trip)):
        return False
    return pd.user_id == actor.id


async def _revoke_strikes(session: AsyncSession, pd: PotentialDriver, now: datetime) -> None:
    """Take back the strikes given for this duty (it was done or excused after all)."""
    for s in await strikes.for_potential_driver(session, pd.id):
        # Also strikes already used up by a penalty week: `revoke` cancels it if still open.
        await strikes.revoke(session, s, now)


async def mark_as_driver(
    session: AsyncSession, pd: PotentialDriver, actor: User, now: datetime
) -> None:
    """`pd` drove the trip; the other potential drivers just rode along.

    Also revokes strikes already given for the trip (the floor speaker may mark it after
    the deadline).

    Raises:
        DutyError: if `actor` may not mark it (see `can_mark_as_driver`).
    """
    if not can_mark_as_driver(pd, actor, now):
        raise DutyError(
            "This duty can't be marked as done (any more): it's not yours, already done, "
            "or past its deadline"
        )
    await _revoke_strikes(session, pd, now)
    pd.status = Status.drove
    pd.completed_at = now
    pd.completed_by_id = actor.id
    pd.disputed_at = None
    for other in pd.trip.potential_drivers:
        # A disputed "done" stays missed with its strike: that person didn't clean.
        if other is pd or other.disputed_at is not None:
            continue
        if other.status in (Status.pending, Status.missed):
            await _revoke_strikes(session, other, now)
            other.status = Status.rode
    await session.flush()


def can_undo(pd: PotentialDriver, actor: User, now: datetime) -> bool:
    """Whether `actor` may take back `pd`'s "Done" now (see `undo_driver`).

    Only whoever marked it (or the floor speaker), within `UNDO_MINUTES`, before the
    trip's deadline, and only if nothing on the trip was disputed: then nobody on it was
    missed or struck before, and undoing is the exact inverse of `mark_as_driver`.
    """
    if pd.status is not Status.drove or pd.completed_at is None:
        return False
    if not (actor.is_admin or pd.completed_by_id == actor.id):
        return False
    if now - pd.completed_at > timedelta(minutes=UNDO_MINUTES) or now >= deadline(pd.trip):
        return False
    return all(x.disputed_at is None for x in pd.trip.potential_drivers)


async def undo_driver(
    session: AsyncSession, pd: PotentialDriver, actor: User, now: datetime
) -> None:
    """Take back a mis-tapped "Done": the trip is open again for everyone on it.

    Raises:
        DutyError: if `actor` may not (any more), see `can_undo`.
    """
    if not can_undo(pd, actor, now):
        raise DutyError(
            f"This can only be undone within {UNDO_MINUTES} minutes, by whoever marked it, "
            "before the deadline"
        )
    for x in pd.trip.potential_drivers:
        if x is pd or x.status is Status.rode:
            x.status = Status.pending
            x.completed_at = None
            x.completed_by_id = None
    await session.flush()


async def excuse(session: AsyncSession, pd: PotentialDriver, actor: User, now: datetime) -> None:
    """Admin only: nobody is blamed, but the person gets no drive either.

    Raises:
        DutyError: if `actor` isn't the floor speaker or the duty isn't open or missed.
    """
    if not actor.is_admin:
        raise DutyError("Only the floor speaker can excuse duties")
    if pd.status not in (Status.pending, Status.missed):
        raise DutyError("Only open or missed duties can be excused")
    await _revoke_strikes(session, pd, now)
    pd.status = Status.excused
    pd.completed_at = now
    pd.completed_by_id = actor.id
    pd.disputed_at = None
    await session.flush()


async def keep_missed(
    session: AsyncSession, pd: PotentialDriver, actor: User, now: datetime
) -> None:
    """Admin only: a missed duty stays missed and its strike stays.

    It no longer needs the floor speaker's attention, and can still be marked done or
    excused later.

    Raises:
        DutyError: if `actor` isn't the floor speaker or the duty isn't missed.
    """
    if not actor.is_admin:
        raise DutyError("Only the floor speaker can decide about missed duties")
    if pd.status is not Status.missed:
        raise DutyError("Only missed duties can be kept as missed")
    pd.kept_missed_at = now
    await session.flush()


async def dispute(session: AsyncSession, pd: PotentialDriver, actor: User, now: datetime) -> None:
    """Admin only: marked as done but not actually done.

    The driver loses the drive and gets a strike; the others stay "rode" and get nothing.

    Raises:
        DutyError: if `actor` isn't the floor speaker or `pd` didn't drive.
    """
    if not actor.is_admin:
        raise DutyError("Only the floor speaker can dispute duties")
    if pd.status is not Status.drove:
        raise DutyError("Only duties marked as done can be disputed")
    pd.status = Status.missed
    pd.disputed_at = now
    await session.flush()
    if pd.user_id is not None:
        await strikes.issue(
            session,
            user_id=pd.user_id,
            reason=(
                f"{pd.trip.duty.label} on {long_date(pd.trip.start_date)} marked as done "
                "but not actually done"
            ),
            potential_driver_id=pd.id,
            issued_by_id=actor.id,
            today=now.date(),
        )


@dataclass
class PreviewEntry:
    """One row of the dashboard preview."""

    start: date
    end: date
    # User ids of the expected potential drivers.
    potential_drivers: list[int]
    is_penalty: bool = False


@dataclass
class Preview:
    """The expected potential drivers of upcoming trips, one plan per possible outcome.

    Who comes up next depends on who drives the open trip (today's, if nobody has driven
    it yet), so there is one plan per pending potential driver of it, keyed by their user
    id. Without an open trip there is a single plan under the key None.
    """

    # First day of the open trip, if any.
    open_start: date | None
    plans: dict[int | None, list[PreviewEntry]]


async def preview(
    session: AsyncSession, duty: Duty, start: date, end: date, today: date
) -> Preview:
    """Expected potential drivers for the trips starting between `start` and `end`.

    Trips that already exist (e.g. penalty weeks) are shown as they are, the others are
    simulated with `carpool.plan`. The dishwasher heroes of `today`'s month are left out
    of its kitchen trips; next month's aren't known yet, so it is planned without any.
    """
    bounds = _trip_bounds_between(duty, start, end)
    existing = {
        t.start_date: t
        for t in await session.scalars(
            select(Trip)
            .where(Trip.duty == duty, Trip.start_date >= start, Trip.start_date <= end)
            .options(selectinload(Trip.potential_drivers))
        )
    }
    todo = [b for b in bounds if b[0] not in existing]
    open_trip = await _open_trip(session, duty, start)
    scenarios = _scenarios(await load_balances(session, duty), open_trip)
    members = await load_members(session, duty, today)

    plans = {}
    for driver, balances in scenarios.items():
        # One list of potential drivers per `todo` trip, in the same order.
        planned = iter(carpool.plan(balances, members, todo, potential_drivers_per_trip(duty)))
        entries = []
        for b in bounds:
            if (trip := existing.get(b[0])) is not None:
                drivers = [pd.user_id for pd in trip.potential_drivers if pd.user_id is not None]
                entries.append(PreviewEntry(*b, drivers, trip.is_penalty))
            else:
                entries.append(PreviewEntry(*b, next(planned)))
        plans[driver] = entries
    return Preview(None if None in scenarios else open_trip.start_date, plans)


def _trip_bounds_between(duty: Duty, start: date, end: date) -> list[tuple[date, date]]:
    """The bounds of `duty`'s trips that start between `start` and `end`."""
    step = timedelta(days=1 if duty is Duty.kitchen else 7)
    day, _ = trip_bounds(duty, start)
    if day < start:  # the trash week already running on `start`
        day += step
    bounds = []
    while day <= end:
        bounds.append(trip_bounds(duty, day))
        day += step
    return bounds


async def _open_trip(session: AsyncSession, duty: Duty, before: date) -> Trip | None:
    """The latest regular trip starting before `before` that nobody has driven yet."""
    earlier = await session.scalars(
        select(Trip)
        .where(
            Trip.duty == duty,
            Trip.is_penalty.is_(False),
            Trip.start_date < before,
            Trip.potential_drivers.any(PotentialDriver.status == Status.pending),
        )
        .order_by(Trip.start_date.desc())
        .options(selectinload(Trip.potential_drivers), selectinload(Trip.riders))
    )
    return next((t for t in earlier if not was_driven(t)), None)


def _scenarios(
    balances: dict[int, Balance], open_trip: Trip | None
) -> dict[int | None, dict[int, Balance]]:
    """The balances as if each pending potential driver of `open_trip` drove it.

    Without one, just `balances` under None. Older trips nobody has driven yet keep their
    shares reserved, as when the real trips are chosen.
    """
    pending = [
        pd
        for pd in (open_trip.potential_drivers if open_trip else [])
        if pd.status is Status.pending and pd.user_id is not None
    ]
    if not pending:
        return {None: balances}
    riders = [r.user_id for r in open_trip.riders]
    released = {pd.user_id: reserved_share(open_trip, pd) for pd in pending}
    return {
        pd.user_id: carpool.assume_driven(
            balances, riders, released, pd.user_id, open_trip.start_date
        )
        for pd in pending
    }
