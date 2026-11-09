"""Seed the database with test residents, optionally with a simulated history.

    uv run python -m scripts.seed                  # 10 residents
    uv run python -m scripts.seed -n 20 --password test1234
    uv run python -m scripts.seed --days 30        # plus a simulated month of duties

Usernames are the lowercase names; existing usernames are skipped, so it is
safe to run more than once. Current trips that nobody has driven yet get their
potential drivers chosen again, so everyone rides along (on startup the app may have
created them while only the floor speaker existed). Afterwards today's duties are assigned.

`--days N` needs a database without history (`docker compose down -v`). It plays the
last N days through the app's own rules: the daily job at DEADLINE_HOUR:05, each duty
done the same evening, the next morning before the deadline, or missed (giving strikes
and penalty weeks), depending on how reliable each resident is, a few absences, and the
dishwasher emptied up to twice a day (some residents do it much more often than others).
Today's kitchen duty is left open. `--random-seed` gives the same history again.
"""

import argparse
import asyncio
import random
import sys
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import SessionLocal
from app.models import Duty, PotentialDriver, Status, Strike, Trip, User
from app.services import dishwasher, duties, jobs, users

# fmt: off
# Star Trek (TOS and TNG) nicknames, so no test resident shares a real resident's name
NAMES = [
    "Picard", "Kirk", "Riker", "Spock", "Data", "McCoy", "Worf", "Scotty", "Troi", "Uhura",
    "Crusher", "Sulu", "Geordi", "Chekov", "Yar", "Guinan", "Chapel", "Wesley", "Rand",
    "Barclay", "Pike", "Pulaski", "Sarek", "Ro", "Lwaxana",
]
# fmt: on

# Of the duties a resident does, this part is marked the same evening, the rest the next
# morning (still before the deadline).
SAME_DAY = 0.85


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), settings.tz)


async def reset_open_trips(session: AsyncSession, today: date) -> int:
    """Delete current and future regular trips where nobody has done anything yet.

    The next daily job creates them again, with everyone who exists by then.
    """
    trips = await session.scalars(
        select(Trip)
        .where(Trip.end_date >= today, Trip.is_penalty.is_(False))
        .options(selectinload(Trip.potential_drivers))
    )
    stale = [t.id for t in trips if all(pd.status is Status.pending for pd in t.potential_drivers)]
    if stale:
        await session.execute(delete(Trip).where(Trip.id.in_(stale)))
        await session.commit()
        session.expire_all()
    return len(stale)


async def tick(session: AsyncSession, now: datetime) -> None:
    """The daily job as if it ran at `now`; its strikes are dated `now` too."""
    last = await session.scalar(select(func.max(Strike.id))) or 0
    await jobs.daily_tick(session, now)
    await session.execute(update(Strike).where(Strike.id > last).values(created_at=now))
    await session.commit()
    session.expire_all()


async def mark(session: AsyncSession, potential_driver_id: int, now: datetime) -> None:
    """The potential driver marks that they drove at `now`, if nobody has driven yet."""
    pd = await duties.get_potential_driver(session, potential_driver_id)
    if pd.status is not Status.pending or pd.trip.driver is not None:
        return
    actor = await session.get(User, pd.user_id)
    await duties.mark_as_driver(session, pd, actor, now)
    await session.commit()
    session.expire_all()


async def decide(
    session: AsyncSession,
    duty: Duty,
    day: date,
    now: datetime,
    reliability: dict[int, float],
    rng: random.Random,
    late: dict[date, list[int]],
) -> None:
    """Whether the trip of `duty` containing `day` gets driven at `now`, tomorrow or never."""
    trip = await duties.get_trip(session, duty, day)
    if trip is None:
        return
    pending = [pd for pd in trip.potential_drivers if pd.status is Status.pending and pd.user_id]
    if not pending:
        return
    # One of them would drive; whether they do depends on how reliable they are.
    driver = rng.choice(pending)
    x = rng.random()
    r = reliability.get(driver.user_id, 0.9)
    if x < r * SAME_DAY:
        await mark(session, driver.id, now)
    elif x < r:
        late.setdefault(now.date() + timedelta(days=1), []).append(driver.id)


async def simulate(session: AsyncSession, days: int, rng: random.Random) -> None:
    """Play the last `days` days, ending now (see the module docstring)."""
    now = settings.now()
    today = now.date()
    start = today - timedelta(days=days)
    await session.execute(
        update(User).where(User.created_at > at(start, 0)).values(created_at=at(start, 0))
    )
    residents = list(await session.scalars(select(User).where(User.active.is_(True))))
    reliability = {u.id: rng.uniform(0.75, 0.98) for u in residents}
    # How often each one empties the dishwasher, relative to the others.
    resident_ids = [u.id for u in residents]
    keen = [rng.choice([0.2, 1, 1, 3]) for _ in residents]

    # A few absences, one of them usually around today.
    non_admins = [u for u in residents if not u.is_admin]
    for u in rng.sample(non_admins, k=min(3, len(non_admins))):
        away_start = start + timedelta(days=rng.randrange(days + 4))
        away_end = away_start + timedelta(days=rng.randrange(4, 10))
        await users.add_away(session, u, away_start, away_end, at(start, 0))
    await session.commit()

    late: dict[date, list[int]] = {}
    day = start
    while day <= today:
        hour = settings.deadline_hour
        steps = [
            # Late ones mark the next morning, an hour before the deadline.
            (at(day, max(hour - 1, 0)), "late"),
            (at(day, hour, 5), "tick"),
            (at(day, 13), "dishwasher"),
            (at(day, 18), "trash"),
            (at(day, 20), "kitchen"),
            (at(day, 21, 30), "dishwasher"),
        ]
        for when, step in steps:
            if when >= now:
                break
            if step == "tick":
                await tick(session, when)
            elif step == "late":
                for potential_driver_id in late.pop(day, []):
                    await mark(session, potential_driver_id, when)
            elif step == "trash" and duties.trip_bounds(Duty.trash, day)[1] == day:
                await decide(session, Duty.trash, day, when, reliability, rng, late)
            elif step == "kitchen" and day < today:
                await decide(session, Duty.kitchen, day, when, reliability, rng, late)
            elif step == "dishwasher" and rng.random() < 0.6:
                # Loaded again: the ticks expire everything loaded before.
                emptier = await session.get(User, rng.choices(resident_ids, weights=keen)[0])
                await dishwasher.mark_cleared(session, emptier, when)
                await session.commit()
        day += timedelta(days=1)

    counts = dict(
        (
            await session.execute(
                select(PotentialDriver.status, func.count()).group_by(PotentialDriver.status)
            )
        ).all()
    )
    strikes = await session.scalar(select(func.count(Strike.id)))
    summary = ", ".join(
        f"{n} {s.value}" for s, n in sorted(counts.items(), key=lambda x: x[0].value)
    )
    print(f"Simulated {days} days from {start:%d.%m.%Y}: {summary}; {strikes} strikes")


async def main(count: int, password: str, days: int, seed: int | None) -> None:
    if count > len(NAMES):
        sys.exit(f"At most {len(NAMES)} residents supported")
    async with SessionLocal() as session:
        # Check before `reset_open_trips`, which commits deletions.
        if days and await session.scalar(select(func.count(Trip.id))):
            sys.exit("--days needs a database without history: docker compose down -v")
        reset = await reset_open_trips(session, settings.today())
        existing = set(await session.scalars(select(User.username)))
        added = 0
        for i in range(count):
            name = NAMES[i]
            username = name.lower()
            if username in existing:
                continue
            await users.create(
                session,
                name=name,
                room_number=f"E1{i + 1:02d}",
                username=username,
                password=password,
            )
            added += 1
        await session.commit()
        if days:
            await simulate(session, days, random.Random(seed))
        await jobs.daily_tick(session, settings.now())
    print(f"{added} residents added ({count - added} already existed), password: {password}")
    if reset:
        print(f"{reset} open trips got their potential drivers chosen again")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--count", type=int, default=10)
    parser.add_argument("--password", default="password123")
    parser.add_argument("--days", type=int, default=0, help="simulate this many past days")
    parser.add_argument("--random-seed", type=int, help="same number, same history")
    args = parser.parse_args()
    asyncio.run(main(args.count, args.password, args.days, args.random_seed))
