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
from app.formatting import long_date
from app.models import Duty, PotentialDriver, Rider, Status, Trip, User
from app.services import carpool, strikes
from app.services.carpool import Balance, Member


class DutyError(Exception):
    """An action on a duty isn't allowed; the message is German and shown as is."""


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
    """Until when the trip can be marked driven; after that it counts as missed."""
    day = trip.end_date + timedelta(days=settings.deadline_days_after)
    return datetime.combine(day, time(settings.deadline_hour), settings.tz)


def choose_at(duty: Duty, day: date) -> datetime:
    """When the potential drivers of the trip containing `day` are chosen.

    At the deadline hour of the trip's first day: right after the previous trip's deadline
    (with the default DEADLINE_DAYS_AFTER=1), so that trip is done or missed by then. That
    way only one trip is ever open, and the preview can be exact for each outcome of it.
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


async def load_members(session: AsyncSession) -> list[Member]:
    """All active users (the carpool's members) with their away periods, for `carpool`."""
    users = await session.scalars(
        select(User).where(User.active.is_(True)).options(selectinload(User.away_periods))
    )
    return [
        Member(
            id=u.id,
            room=u.room_number,
            away=tuple((a.start_date, a.end_date) for a in u.away_periods),
        )
        for u in users
    ]


def drove(trip: Trip, pd: PotentialDriver) -> bool:
    """Whether `pd` drove `trip` and earns the drive. Penalty trips earn nothing."""
    return not trip.is_penalty and pd.status is Status.drove


def was_driven(trip: Trip) -> bool:
    """Whether `trip` counts for fairness: a regular trip that someone drove."""
    return not trip.is_penalty and any(pd.status is Status.drove for pd in trip.potential_drivers)


def pending_share(trip: Trip, pd: PotentialDriver) -> Fraction:
    """While nobody has driven a trip, its pending potential drivers share it.

    Counts only for choosing (`Balance.pending_share`), so tomorrow's trip doesn't choose
    them again before anyone drove; their score doesn't change.
    """
    if trip.is_penalty or pd.status is not Status.pending:
        return Fraction(0)
    if any(x.status is Status.drove for x in trip.potential_drivers):
        return Fraction(0)
    return Fraction(1, sum(x.status is Status.pending for x in trip.potential_drivers))


async def load_balances(session: AsyncSession, duty: Duty) -> dict[int, Balance]:
    """Every user's balance in the carpool of `duty`, computed from all trips so far.

    Balances are never stored; they are always recomputed from the history. As in the
    carpool algorithm, a trip counts only once someone drove it: one that was missed,
    excused or disputed (or is still open) charges nobody. So the scores always sum to 0,
    apart from the scores of residents who moved out.
    """
    balances: dict[int, Balance] = {}

    trips = (
        await session.scalars(
            select(Trip)
            .where(Trip.duty == duty)
            .options(selectinload(Trip.riders), selectinload(Trip.potential_drivers))
        )
    ).all()
    for trip in trips:
        if was_driven(trip):
            carpool.charge_riders(balances, [r.user_id for r in trip.riders])
        for pd in trip.potential_drivers:
            if pd.user_id is None:
                continue
            if drove(trip, pd):
                carpool.credit_driver(balances, pd.user_id, trip.start_date)
            carpool.reserve_share(balances, pd.user_id, pending_share(trip, pd))
    return balances


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
    before enough residents existed) is topped up.
    """
    existing = await get_trip(session, duty, day)
    n = potential_drivers_per_trip(duty)
    if existing is not None:
        already_driven = any(pd.status is Status.drove for pd in existing.potential_drivers)
        if existing.is_penalty or already_driven or len(existing.potential_drivers) >= n:
            return existing
        await _top_up(session, existing, n - len(existing.potential_drivers))
        return await get_trip(session, duty, day)

    start, end = trip_bounds(duty, day)
    members = await load_members(session)
    rooms = {m.id: m.room for m in members}
    riders = [m.id for m in members if m.rides(start, end)]

    balances = await load_balances(session, duty)
    carpool.charge_riders(balances, riders)
    chosen = carpool.choose_potential_drivers(balances, riders, n, rooms)

    trip = Trip(duty=duty, start_date=start, end_date=end, is_penalty=False)
    trip.riders = [Rider(user_id=uid) for uid in riders]
    trip.potential_drivers = [PotentialDriver(user_id=uid) for uid in chosen]
    session.add(trip)
    await session.flush()
    return await get_trip(session, duty, day)


async def _top_up(session: AsyncSession, trip: Trip, missing: int) -> None:
    """Add `missing` potential drivers to an existing trip, including residents added since."""
    members = await load_members(session)
    await session.refresh(trip, ["riders"])
    known = {r.user_id for r in trip.riders}
    for m in members:
        if m.id not in known and m.rides(trip.start_date, trip.end_date):
            trip.riders.append(Rider(user_id=m.id))
    await session.flush()
    await _add_potential_drivers(session, trip, missing, {m.id: m.room for m in members})


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


async def fill_current(session: AsyncSession, today: date) -> None:
    """Make sure today's kitchen trip and this week's trash trip have potential drivers."""
    for duty in Duty:
        await materialize(session, duty, today)


async def reconcile(session: AsyncSession, today: date) -> None:
    """Replace potential drivers of current/upcoming trips who no longer ride on them.

    Called after someone marks themselves away, is deactivated or deleted. They are
    removed from the trip's riders too, so they aren't charged a share.
    """
    members = {m.id: m for m in await load_members(session)}
    rooms = {uid: m.room for uid, m in members.items()}

    def rides(user_id: int | None, trip: Trip) -> bool:
        member = members.get(user_id)
        return member is not None and member.rides(trip.start_date, trip.end_date)

    trips = (
        await session.scalars(
            select(Trip)
            .where(Trip.end_date >= today, Trip.is_penalty.is_(False))
            .options(selectinload(Trip.riders), selectinload(Trip.potential_drivers))
        )
    ).all()
    for trip in trips:
        gone = [
            pd
            for pd in trip.potential_drivers
            if pd.status is Status.pending and not rides(pd.user_id, trip)
        ]
        if not gone:
            continue
        for pd in gone:
            trip.potential_drivers.remove(pd)
            await session.delete(pd)
        trip.riders = [r for r in trip.riders if rides(r.user_id, trip)]
        await session.flush()
        await _add_potential_drivers(session, trip, len(gone), rooms)


async def process_overdue(session: AsyncSession, now: datetime) -> list[PotentialDriver]:
    """Mark pending potential drivers past their deadline as missed and strike them."""
    pending = (
        await session.scalars(
            select(PotentialDriver)
            .join(PotentialDriver.trip)
            .where(PotentialDriver.status == Status.pending)
            .options(selectinload(PotentialDriver.trip))
        )
    ).all()
    missed = [pd for pd in pending if deadline(pd.trip) <= now]
    for pd in missed:
        pd.status = Status.missed
        if pd.user_id is not None:
            label = pd.trip.duty.label
            await strikes.issue(
                session,
                user_id=pd.user_id,
                reason=f"{label} on {long_date(pd.trip.start_date)} not marked as done",
                auto=True,
                potential_driver_id=pd.id,
                today=now.date(),
            )
    await session.flush()
    return missed


async def get_potential_driver(session: AsyncSession, potential_driver_id: int) -> PotentialDriver:
    """The potential driver with their trip and the trip's other potential drivers loaded.

    Raises:
        DutyError: if there is no such potential driver.
    """
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
    floor speaker: any duty at any time, as long as nobody has driven the trip.
    """
    # Only one person drives a trip: once somebody drove it, it's closed.
    if any(x.status is Status.drove for x in pd.trip.potential_drivers):
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
        if s.is_active:
            s.revoked_at = now


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
        raise DutyError("You can't tick off this duty")
    await _revoke_strikes(session, pd, now)
    pd.status = Status.drove
    pd.completed_at = now
    pd.completed_by_id = actor.id
    pd.disputed_at = None
    for other in pd.trip.potential_drivers:
        if other is not pd and other.status in (Status.pending, Status.missed):
            await _revoke_strikes(session, other, now)
            other.status = Status.rode
    await session.flush()


async def excuse(session: AsyncSession, pd: PotentialDriver, actor: User, now: datetime) -> None:
    """Admin only: nobody is blamed, but the person gets no drive either.

    Raises:
        DutyError: if `actor` isn't the floor speaker.
    """
    if not actor.is_admin:
        raise DutyError("Only the floor speaker can excuse duties")
    await _revoke_strikes(session, pd, now)
    pd.status = Status.excused
    pd.completed_at = now
    pd.completed_by_id = actor.id
    await session.flush()


async def dispute(session: AsyncSession, pd: PotentialDriver, actor: User, now: datetime) -> None:
    """Admin only: marked as driven but not actually done. The driver gets a strike.

    The driver loses the drive; the others stay "rode" and get nothing.

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


async def preview(session: AsyncSession, duty: Duty, start: date, end: date) -> Preview:
    """Expected potential drivers for the trips starting between `start` and `end`.

    Trips that already exist (e.g. penalty weeks) are shown as they are; the others are
    simulated with `carpool.plan`, once per pending potential driver of the open trip, as
    if that one drove it (`carpool.assume_driven`). Older trips nobody has driven yet (e.g.
    yesterday's before the deadline) count as `load_balances` has them: their potential
    drivers keep their share reserved, as when the real trips are chosen.
    """
    n = potential_drivers_per_trip(duty)
    step = timedelta(days=1 if duty is Duty.kitchen else 7)
    # Trash weeks start on Monday: skip the week that is already running on `start`.
    first, _ = trip_bounds(duty, start)
    if first < start:
        first += step

    existing = {
        t.start_date: t
        for t in await session.scalars(
            select(Trip)
            .where(Trip.duty == duty, Trip.start_date >= first, Trip.start_date <= end)
            .options(selectinload(Trip.potential_drivers))
        )
    }
    bounds = []
    day = first
    while day <= end:
        bounds.append(trip_bounds(duty, day))
        day += step
    todo = [b for b in bounds if b[0] not in existing]

    # The open trip: the latest regular one before the window that nobody has driven yet.
    earlier = await session.scalars(
        select(Trip)
        .where(
            Trip.duty == duty,
            Trip.is_penalty.is_(False),
            Trip.start_date < first,
            Trip.potential_drivers.any(PotentialDriver.status == Status.pending),
        )
        .order_by(Trip.start_date.desc())
        .options(selectinload(Trip.potential_drivers), selectinload(Trip.riders))
    )
    open_trip = next((t for t in earlier if not was_driven(t)), None)
    pending = (
        [pd for pd in open_trip.potential_drivers if pd.status is Status.pending and pd.user_id]
        if open_trip
        else []
    )

    balances = await load_balances(session, duty)
    members = await load_members(session)
    scenarios: dict[int | None, dict[int, Balance]] = {None: balances}
    if pending:
        riders = [r.user_id for r in open_trip.riders]
        released = {pd.user_id: pending_share(open_trip, pd) for pd in pending}
        scenarios = {
            pd.user_id: carpool.assume_driven(
                balances, riders, released, pd.user_id, open_trip.start_date
            )
            for pd in pending
        }

    plans = {}
    for driver, scenario in scenarios.items():
        # plan() returns one list of potential drivers per `todo` trip, in the same order.
        planned = iter(carpool.plan(scenario, members, todo, n))
        entries = []
        for b in bounds:
            if b[0] in existing:
                t = existing[b[0]]
                drivers = [pd.user_id for pd in t.potential_drivers if pd.user_id]
                entries.append(PreviewEntry(*b, drivers, t.is_penalty))
            else:
                entries.append(PreviewEntry(*b, next(planned)))
        plans[driver] = entries
    return Preview(open_trip.start_date if pending else None, plans)
