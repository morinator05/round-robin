"""Seed the database with test residents, optionally with a simulated history.

    uv run python scripts/seed.py                  # 15 residents
    uv run python scripts/seed.py -n 20 --password test1234
    uv run python scripts/seed.py --days 30        # plus a simulated month of duties

Usernames are the lowercase first names; existing usernames are skipped, so it is
safe to run more than once. Current trips that nobody has driven yet get their
potential drivers chosen again, so everyone rides along (on startup the app may have
created them while only the Flursprecher existed). Afterwards today's duties are assigned.

`--days N` needs a database without history (`docker compose down -v`). It plays the
last N days through the app's own rules: the daily job at 00:05 and 12:05, each duty
done the same evening, the next morning, or missed (giving strikes and penalty weeks),
depending on how reliable each resident is, and a few absences. Today's kitchen duty
is left open. `--random-seed` gives the same history again.
"""

import argparse
import asyncio
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Duty, PotentialDriver, Status, Strike, Trip, User  # noqa: E402
from app.services import duties, jobs, users  # noqa: E402

# fmt: off
FIRST_NAMES = [
    "Anna", "Ben", "Clara", "David", "Emma", "Felix", "Greta", "Hannes", "Ida", "Jonas",
    "Kim", "Lukas", "Mia", "Noah", "Olivia", "Paul", "Quinn", "Rosa", "Simon", "Tara",
    "Uwe", "Vera", "Wim", "Xenia", "Yusuf", "Zoe",
]
LAST_NAMES = [
    "Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker",
    "Schulz", "Hoffmann", "Koch", "Richter", "Klein", "Wolf", "Neumann", "Schwarz",
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
    driven = any(x.status is Status.drove for x in pd.trip.potential_drivers)
    if pd.status is not Status.pending or driven:
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

    # A few absences, one of them usually around today.
    for u in rng.sample([u for u in residents if not u.is_admin], k=min(3, len(residents) - 1)):
        away_start = start + timedelta(days=rng.randrange(days + 4))
        away_end = away_start + timedelta(days=rng.randrange(4, 10))
        await users.add_away(session, u, away_start, away_end, start)
    await session.commit()

    late: dict[date, list[int]] = {}
    day = start
    while day <= today:
        steps = [
            (at(day, 0, 5), "tick"),
            (at(day, 9), "late"),
            (at(day, 12, 5), "tick"),
            (at(day, 18), "trash"),
            (at(day, 20), "kitchen"),
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
    if count > len(FIRST_NAMES):
        sys.exit(f"At most {len(FIRST_NAMES)} residents supported")
    async with SessionLocal() as session:
        reset = await reset_open_trips(session, settings.today())
        if days and await session.scalar(select(func.count(Trip.id))):
            sys.exit("--days needs a database without history: docker compose down -v")
        existing = set(await session.scalars(select(User.username)))
        added = 0
        for i in range(count):
            first = FIRST_NAMES[i]
            username = first.lower()
            if username in existing:
                continue
            await users.create(
                session,
                name=f"{first} {LAST_NAMES[i % len(LAST_NAMES)]}",
                room_number=f"A4{i + 1:02d}",
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
    parser.add_argument("-n", "--count", type=int, default=15)
    parser.add_argument("--password", default="passwort123")
    parser.add_argument("--days", type=int, default=0, help="simulate this many past days")
    parser.add_argument("--random-seed", type=int, help="same number, same history")
    args = parser.parse_args()
    asyncio.run(main(args.count, args.password, args.days, args.random_seed))
