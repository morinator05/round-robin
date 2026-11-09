"""Shared test helpers: fixed dates, residents and duties, and reading pages."""

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import (
    AwayPeriod,
    DishwasherClear,
    Duty,
    PotentialDriver,
    Strike,
    Trip,
    User,
)
from app.services import duties, jobs, strikes, users

MONDAY = date(2026, 11, 2)
TUESDAY = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)
NEXT_MONDAY = MONDAY + timedelta(days=7)
NOVEMBER = date(2026, 11, 1)  # a Sunday; MONDAY is the day after
OCTOBER = date(2026, 10, 20)
PASSWORD = "secret123"


def at(day: date, hour: int = 9, minute: int = 0) -> datetime:
    """`day` at `hour`:`minute` in the app's time zone."""
    return datetime.combine(day, time(hour, minute), settings.tz)


async def make_users(session, n=15, admin=True) -> list[User]:
    """`n` residents p1..pn in rooms E101.. (password `PASSWORD`); p1 is the floor speaker."""
    created = [
        await users.create(
            session,
            name=f"Person {i}",
            room_number=f"E1{i:02d}",
            username=f"p{i}",
            password=PASSWORD,
            is_admin=admin and i == 1,
        )
        for i in range(1, n + 1)
    ]
    await session.commit()
    return created


@dataclass
class Pair:
    """A day's kitchen trip and its two potential drivers, `first` being `p1`'s."""

    trip: Trip
    first: PotentialDriver
    second: PotentialDriver
    p1: User
    p2: User

    @property
    def ids(self) -> set[int]:
        return {self.p1.id, self.p2.id}


async def kitchen_pair(session, people, day=MONDAY, admin_off=False) -> Pair:
    """Run the daily job for `day` and return its kitchen pair.

    `admin_off` keeps the floor speaker (people[0]) off that day, so they're an outsider.
    """
    if admin_off:
        await users.add_away(session, people[0], day, day, day)
    await jobs.daily_tick(session, at(day))
    trip = await duties.get_trip(session, Duty.kitchen, day)
    by_id = {p.id: p for p in people}
    # Loaded like the routes do, so each potential driver keeps its trip loaded.
    first, second = [
        await duties.get_potential_driver(session, pd.id) for pd in trip.potential_drivers
    ]
    # `get_potential_driver` locks the trip's rows until the transaction ends; requests
    # through the app (another session) would wait for them.
    await session.commit()
    return Pair(trip, first, second, by_id[first.user_id], by_id[second.user_id])


async def give_strikes(session, user_id: int, n=None, today=MONDAY) -> list[Strike]:
    """`n` strikes (the threshold by default) for `user_id`, by hand, with reasons r0, r1.."""
    n = settings.strike_threshold if n is None else n
    return [
        await strikes.issue(session, user_id=user_id, reason=f"r{i}", today=today) for i in range(n)
    ]


async def reload_trip(session, duty: Duty, day: date) -> Trip:
    """The trip of `duty` on `day` as it is in the database now, riders included."""
    return await session.scalar(
        select(Trip)
        .where(Trip.duty == duty, Trip.start_date <= day, Trip.end_date >= day)
        .options(selectinload(Trip.riders), selectinload(Trip.potential_drivers))
        .execution_options(populate_existing=True)
    )


async def penalty_trips(session) -> list[Trip]:
    """All penalty weeks, with their potential drivers, as they are in the database now."""
    return list(
        await session.scalars(
            select(Trip)
            .where(Trip.is_penalty.is_(True))
            .order_by(Trip.start_date)
            .options(selectinload(Trip.potential_drivers))
            .execution_options(populate_existing=True)
        )
    )


async def plans(session, today: date, days=14, duty=Duty.kitchen) -> dict:
    """The preview's plans from the day after `today` on, keyed by who drives the open trip."""
    start = today + timedelta(days=1)
    preview = await duties.preview(session, duty, start, today + timedelta(days=days), today)
    return preview.plans


def planned(plans: dict) -> set[int]:
    """Everybody who is in any row of any of `plans`."""
    return {uid for plan in plans.values() for e in plan for uid in e.potential_drivers}


# --- pages ------------------------------------------------------------------


async def login(client, username: str, password: str = PASSWORD) -> None:
    """Log in as `username`; fails the test if that doesn't work."""
    r = await client.post("/login", data={"username": username, "password": password})
    assert r.status_code == 303 and r.headers["location"] == "/", f"login as {username} failed"


def words(page: str | httpx.Response) -> str:
    """The visible text of `page`: no tags, entities decoded, whitespace runs as one space."""
    text = page.text if isinstance(page, httpx.Response) else page
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", text)).split())


def section(page: str | httpx.Response, id: str) -> str:
    """The HTML of the element with `id`, up to the end of its section."""
    text = page.text if isinstance(page, httpx.Response) else page
    start = text.index(f'id="{id}"')
    return text[start : text.index("</section>", start)]


async def follow(client, r: httpx.Response) -> str:
    """The visible text of the page `r` redirects to."""
    assert r.status_code == 303, r.status_code
    return words(await client.get(r.headers["location"]))


def assert_no_personal_data(text: str, people) -> None:
    """None of the residents' names, usernames or rooms appear in `text`."""
    for p in people:
        for value in (p.name, p.first_name, p.room_number, p.username):
            assert value not in text, f"{value!r} visible without login"


def assert_sent_to_login(r: httpx.Response, path: str, people) -> None:
    """`r` sends the browser to the login page and shows nothing personal.

    Pages (GET) are come back to after logging in; after a form POST that would be a GET
    of a POST-only route, so it goes to the dashboard. htmx requests get `HX-Redirect`.
    """
    back = path if r.request.method == "GET" else "/"
    if r.request.headers.get("HX-Request"):
        assert r.status_code == 200, f"{r.request.method} {path}: {r.status_code}"
        assert r.headers["hx-redirect"] == f"/login?next={back}"
    else:
        assert r.status_code == 303, f"{r.request.method} {path}: {r.status_code}, not a redirect"
        assert r.headers["location"] == f"/login?next={back}"
    assert_no_personal_data(r.text, people)


async def is_logged_in(client) -> bool:
    r = await client.get("/me")
    if r.status_code == 303:
        assert r.headers["location"].startswith("/login"), r.headers["location"]
        return False
    assert r.status_code == 200
    return True


async def row_counts(session) -> dict:
    """How many rows each table has, and potential drivers per status."""
    counts = {
        model.__name__: await session.scalar(select(func.count()).select_from(model))
        for model in (User, AwayPeriod, Strike, Trip, PotentialDriver, DishwasherClear)
    }
    statuses = await session.execute(
        select(PotentialDriver.status, func.count()).group_by(PotentialDriver.status)
    )
    return counts | {status: n for status, n in statuses.all()}


def all_routes(routes):
    """`routes` with included routers expanded: FastAPI keeps those as one wrapper each."""
    for route in routes:
        if (sub := getattr(route, "original_router", None)) is not None:
            yield from all_routes(sub.routes)
        else:
            yield route
