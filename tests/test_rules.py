"""Rules against a real database: assigning, deadlines, strikes, moving out, web pages."""

import asyncio
import base64
import json
import random
import re
from datetime import date, datetime, time, timedelta
from fractions import Fraction

import httpx
import pytest
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app import web
from app.config import settings
from app.main import app
from app.models import Duty, PotentialDriver, Status, Trip
from app.services import duties, jobs, strikes, users
from app.services.duties import DutyError
from app.services.login_throttle import LoginThrottle
from app.services.users import UserError
from app.web import person_html

MONDAY = date(2026, 11, 2)


def at(day: date, hour: int = 9) -> datetime:
    """`day` at `hour` in the app's time zone."""
    return datetime.combine(day, time(hour), settings.tz)


async def make_users(session, n=15, admin=True):
    """`n` residents p1..pn (password "secret123"); p1 is the floor speaker if `admin`."""
    created = []
    for i in range(1, n + 1):
        created.append(
            await users.create(
                session,
                name=f"Person {i}",
                room_number=f"E1{i:02d}",
                username=f"p{i}",
                password="secret123",
                is_admin=admin and i == 1,
            )
        )
    await session.commit()
    return created


async def test_tick_assigns_kitchen_and_trash(session):
    await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    trash = await duties.get_trip(session, Duty.trash, MONDAY + timedelta(days=3))
    assert len(kitchen.potential_drivers) == 2
    assert len(trash.potential_drivers) == 1
    assert trash.start_date == MONDAY and trash.end_date == MONDAY + timedelta(days=6)

    # Idempotent.
    await jobs.daily_tick(session, at(MONDAY, 13))
    assert len((await session.scalars(select(Trip))).all()) == 2


async def test_missed_kitchen_strikes_both(session):
    await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    chosen = [pd.user_id for pd in kitchen.potential_drivers]

    # Before the deadline (the next day at the deadline hour) nothing happens.
    hour = settings.deadline_hour
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), hour - 1))
    assert all(pd.status is Status.pending for pd in kitchen.potential_drivers)

    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), hour))
    await session.refresh(kitchen, ["potential_drivers"])
    assert all(pd.status is Status.missed for pd in kitchen.potential_drivers)
    for uid in chosen:
        assert len(await strikes.active_strikes(session, uid)) == 1


async def test_three_strikes_give_penalty_in_next_free_week(session):
    people = await make_users(session)
    admin, a, b = people[0], people[1], people[2]
    await jobs.daily_tick(session, at(MONDAY))
    for _ in range(3):
        await strikes.issue(session, user_id=a.id, reason="x", issued_by_id=admin.id, today=MONDAY)
    for _ in range(3):
        await strikes.issue(session, user_id=b.id, reason="x", issued_by_id=admin.id, today=MONDAY)
    await session.commit()

    penalties = (
        await session.scalars(
            select(Trip).where(Trip.is_penalty.is_(True)).order_by(Trip.start_date)
        )
    ).all()
    assert [p.start_date for p in penalties] == [
        MONDAY + timedelta(days=7),
        MONDAY + timedelta(days=14),
    ]
    assert len(await strikes.active_strikes(session, a.id)) == 0

    # The penalty week replaces the regular trash duty.
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=7)))
    trip = await duties.get_trip(session, Duty.trash, MONDAY + timedelta(days=7))
    assert trip.is_penalty and [x.user_id for x in trip.potential_drivers] == [a.id]


async def test_penalty_does_not_count_as_regular_trash(session):
    people = await make_users(session, n=3)
    a = people[1]
    for _ in range(3):
        await strikes.issue(session, user_id=a.id, reason="x", today=MONDAY)
    balances = await duties.load_balances(session, Duty.trash)
    assert balances[a.id].drives == 0


async def kitchen_pair(session, people, day=MONDAY):
    """Run the daily job for `day`; returns the kitchen trip, its potential drivers, people."""
    await jobs.daily_tick(session, at(day))
    kitchen = await duties.get_trip(session, Duty.kitchen, day)
    by_id = {p.id: p for p in people}
    # Loaded like the routes do, so each potential driver keeps its trip loaded even if the
    # caller drops `kitchen` (otherwise `pd.trip` may need a lazy load, which async can't do).
    first, second = [
        await duties.get_potential_driver(session, pd.id) for pd in kitchen.potential_drivers
    ]
    return kitchen, first, second, by_id[first.user_id], by_id[second.user_id]


async def test_mark_as_driver_permissions(session):
    people = await make_users(session)
    admin = people[0]
    # Keep the admin off today's duty, so the outsider/admin checks are meaningful.
    await users.add_away(session, admin, MONDAY, MONDAY + timedelta(days=1), MONDAY)
    _, first, second, p1, p2 = await kitchen_pair(session, people)
    outsider = next(p for p in people if p.id not in {p1.id, p2.id} and not p.is_admin)

    with pytest.raises(DutyError):
        await duties.mark_as_driver(session, first, outsider, at(MONDAY, 20))
    # Nobody can mark someone else's duty.
    with pytest.raises(DutyError):
        await duties.mark_as_driver(session, first, p2, at(MONDAY, 20))

    # Too late for a resident, but the floor speaker can still do it.
    tuesday = MONDAY + timedelta(days=1)
    _, t_first, _, t1, _ = await kitchen_pair(session, people, tuesday)
    with pytest.raises(DutyError):
        await duties.mark_as_driver(session, t_first, t1, at(tuesday + timedelta(days=1), 12))
    await duties.mark_as_driver(session, t_first, admin, at(tuesday + timedelta(days=1), 12))
    assert t_first.status is Status.drove


async def test_first_driver_closes_the_trip(session):
    people = await make_users(session)
    kitchen, first, second, p1, p2 = await kitchen_pair(session, people)

    await duties.mark_as_driver(session, second, p2, at(MONDAY, 20))
    assert second.status is Status.drove and second.completed_by_id == p2.id
    assert first.status is Status.rode
    with pytest.raises(DutyError):
        await duties.mark_as_driver(session, first, p1, at(MONDAY, 21))
    await session.commit()

    # Only the one who cleaned gets credit; the partner gets neither credit nor strike.
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p2.id].drives == 1 and balances[p1.id].drives == 0
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=2)))
    assert await strikes.active_strikes(session, p1.id) == []


async def test_undriven_trip_is_shared_for_choosing_but_not_for_score(session):
    people = await make_users(session)
    _, _, _, p1, p2 = await kitchen_pair(session, people)
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p1.id].pending_share == balances[p2.id].pending_share == Fraction(1, 2)
    # The score shown only changes once someone drives.
    assert balances[p1.id].drives == balances[p2.id].drives == 0

    # Tomorrow is chosen before anyone drove today; today's pair isn't chosen again.
    tomorrow = await duties.materialize(session, Duty.kitchen, MONDAY + timedelta(days=1))
    assert {pd.user_id for pd in tomorrow.potential_drivers}.isdisjoint({p1.id, p2.id})


async def test_trip_nobody_drove_does_not_count(session):
    """A trip nobody drove (missed, excused, disputed) counts for nobody."""
    people = await make_users(session)
    await kitchen_pair(session, people)
    # Monday's and Tuesday's kitchen duty are missed by Wednesday afternoon.
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1)))
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=2), 13))

    balances = await duties.load_balances(session, Duty.kitchen)
    assert all(b.score == 0 for b in balances.values())


async def test_scores_sum_to_zero(session):
    """All scores sum to 0: what the driver is ahead, the riders are behind."""
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, _, p1, _ = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))

    balances = await duties.load_balances(session, Duty.kitchen)
    assert sum(b.score for b in balances.values()) == 0
    assert balances[p1.id].score == 1 - Fraction(1, 14)


async def test_disputed_trip_stops_counting(session):
    """A disputed trip wasn't driven after all, so it counts for nobody."""
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, _, p1, _ = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))

    await duties.dispute(session, first, admin, at(MONDAY, 21))
    balances = await duties.load_balances(session, Duty.kitchen)
    assert all(b.score == 0 for b in balances.values())


async def test_preview_has_one_plan_per_open_potential_driver(session):
    people = await make_users(session)
    _, first, _, p1, p2 = await kitchen_pair(session, people)

    async def plans():
        preview = await duties.preview(
            session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=30)
        )
        return preview.plans

    before = await plans()
    assert set(before) == {p1.id, p2.id}
    # Whoever doesn't clean is up again tomorrow; whoever does, much later.
    for driver, other in [(p1, p2), (p2, p1)]:
        plan = before[driver.id]
        assert other.id in plan[0].potential_drivers
        assert all(driver.id not in e.potential_drivers for e in plan[:7])

    # Once it's done, the outcome is known: a single plan.
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))
    after = await plans()
    assert list(after) == [None]
    assert after[None] == before[p1.id]


async def test_day_is_chosen_only_after_the_previous_deadline(session):
    await make_users(session)
    hour = settings.deadline_hour
    tuesday = MONDAY + timedelta(days=1)
    await jobs.daily_tick(session, at(MONDAY, hour))
    monday_trip = await duties.get_trip(session, Duty.kitchen, MONDAY)

    # Tuesday before the deadline hour: Monday's duty is still the current one.
    early = at(tuesday, hour - 1)
    await jobs.daily_tick(session, early)
    assert await duties.get_trip(session, Duty.kitchen, tuesday) is None
    assert (await duties.current_trip(session, Duty.kitchen, early)).id == monday_trip.id

    # Right after it: Monday's is settled (missed here) before Tuesday's is chosen.
    await jobs.daily_tick(session, at(tuesday, hour))
    await session.refresh(monday_trip, ["potential_drivers"])
    assert all(pd.status is Status.missed for pd in monday_trip.potential_drivers)
    tuesday_trip = await duties.get_trip(session, Duty.kitchen, tuesday)
    assert len(tuesday_trip.potential_drivers) == 2


async def history(session, people, days: int, seed: int) -> date:
    """`days` days of kitchen duty from MONDAY: mostly done by a random one, sometimes
    missed, one resident away for a while and one moving out. Returns the next day."""
    rng = random.Random(seed)
    by_id = {p.id: p for p in people}
    hour = settings.deadline_hour
    for i in range(days):
        day = MONDAY + timedelta(days=i)
        await jobs.daily_tick(session, at(day, hour))
        if i == 2:
            await users.add_away(session, people[5], day, day + timedelta(days=3), day)
        if i == 4:
            await users.delete(session, people[9], day)
        await session.commit()
        trip = await duties.get_trip(session, Duty.kitchen, day)
        pending = [pd for pd in trip.potential_drivers if pd.status is Status.pending]
        if pending and rng.random() > 0.15:
            pd = await duties.get_potential_driver(session, rng.choice(pending).id)
            await duties.mark_as_driver(session, pd, by_id[pd.user_id], at(day, 20))
            await session.commit()
    return MONDAY + timedelta(days=days)


# Seed 3 gives a pair with different scores (the fair choice usually picks equal ones).
@pytest.mark.parametrize("seed", [3])
@pytest.mark.parametrize("which", [0, 1])
async def test_each_plan_is_what_really_happens(session, seed, which):
    """With the pair's scores apart, each plan is exactly what gets chosen tomorrow once
    that one marks today's duty as done (the old preview averaged the pair)."""
    people = await make_users(session)
    by_id = {p.id: p for p in people}
    today = await history(session, people, 12, seed)
    hour = settings.deadline_hour
    await jobs.daily_tick(session, at(today, hour))
    trip = await duties.get_trip(session, Duty.kitchen, today)
    pair = [pd for pd in trip.potential_drivers if pd.status is Status.pending]
    balances = await duties.load_balances(session, Duty.kitchen)
    scores = {pd.user_id: balances[pd.user_id].score for pd in pair if pd.user_id in balances}
    assert len(pair) == 2 and len(set(scores.values())) == 2

    tomorrow = today + timedelta(days=1)
    before = await duties.preview(session, Duty.kitchen, tomorrow, today + timedelta(days=14))
    assert set(before.plans) == {pd.user_id for pd in pair}

    driver = pair[which]
    pd = await duties.get_potential_driver(session, driver.id)
    await duties.mark_as_driver(session, pd, by_id[driver.user_id], at(today, 20))
    await session.commit()
    after = await duties.preview(session, Duty.kitchen, tomorrow, today + timedelta(days=14))
    assert after.plans[None] == before.plans[driver.user_id]

    await jobs.daily_tick(session, at(tomorrow, hour))
    chosen = await duties.get_trip(session, Duty.kitchen, tomorrow)
    real = [x.user_id for x in chosen.potential_drivers]
    assert real == before.plans[driver.user_id][0].potential_drivers


@pytest.mark.parametrize("which", [0, 1])
async def test_morning_preview_follows_yesterdays_pair(session, which):
    """Before the deadline hour yesterday's duty is the open one; today's row of the plan
    for whoever marks it is what gets chosen at the deadline hour."""
    people = await make_users(session)
    by_id = {p.id: p for p in people}
    hour = settings.deadline_hour
    tuesday = MONDAY + timedelta(days=1)
    await jobs.daily_tick(session, at(MONDAY, hour))
    monday = await duties.get_trip(session, Duty.kitchen, MONDAY)

    preview = await duties.preview(session, Duty.kitchen, tuesday, MONDAY + timedelta(days=14))
    assert preview.open_start == MONDAY
    assert list(preview.plans) == [pd.user_id for pd in monday.potential_drivers]

    driver = monday.potential_drivers[which]
    pd = await duties.get_potential_driver(session, driver.id)
    await duties.mark_as_driver(session, pd, by_id[driver.user_id], at(tuesday, hour - 1))
    await session.commit()
    await jobs.daily_tick(session, at(tuesday, hour))
    chosen = await duties.get_trip(session, Duty.kitchen, tuesday)
    real = [x.user_id for x in chosen.potential_drivers]
    assert real == preview.plans[driver.user_id][0].potential_drivers


async def test_dashboard_before_the_deadline_hour_shows_yesterday(session, client, monkeypatch):
    people = await make_users(session)
    hour = settings.deadline_hour
    tuesday = MONDAY + timedelta(days=1)
    await jobs.daily_tick(session, at(MONDAY, hour))
    await session.commit()
    monkeypatch.setattr(type(settings), "now", lambda self: at(tuesday, hour - 1))
    text = (await client.get("/")).text
    card = text[
        text.index('id="card-kitchen"') : text.index("</section>", text.index('id="card-kitchen"'))
    ]
    assert "yesterday" in card
    assert "The plan depends on who cleaned yesterday" in text
    assert await duties.get_trip(session, Duty.kitchen, tuesday) is None
    assert people


async def test_partner_who_did_not_drive_is_chosen_next(session):
    people = await make_users(session)
    _, first, _, p1, p2 = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))
    tomorrow = await duties.materialize(session, Duty.kitchen, MONDAY + timedelta(days=1))
    ids = {pd.user_id for pd in tomorrow.potential_drivers}
    assert p2.id in ids and p1.id not in ids


async def test_dispute_gives_strike_and_removes_the_drive(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, second, p1, p2 = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))

    with pytest.raises(DutyError):
        await duties.dispute(session, first, p2, at(MONDAY, 21))
    await duties.dispute(session, first, admin, at(MONDAY, 21))
    await session.commit()

    assert first.status is Status.missed and first.disputed_at is not None
    assert second.status is Status.rode
    [strike] = await strikes.active_strikes(session, p1.id)
    assert strike.issued_by_id == admin.id and strike.potential_driver_id == first.id
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p1.id].drives == 0
    # The deadline passing later doesn't add a second strike.
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=2)))
    assert len(await strikes.active_strikes(session, p1.id)) == 1


async def test_admin_marking_missed_driver_revokes_strike(session):
    people = await make_users(session)
    admin = people[0]
    await jobs.daily_tick(session, at(MONDAY))
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), 13))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    pd = await duties.get_potential_driver(session, kitchen.potential_drivers[0].id)
    assert pd.status is Status.missed
    await duties.mark_as_driver(session, pd, admin, at(MONDAY + timedelta(days=2)))
    assert len(await strikes.active_strikes(session, pd.user_id)) == 0


async def test_away_replaces_pending_potential_driver(session):
    people = await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    leaving_id = kitchen.potential_drivers[0].user_id
    leaving = next(p for p in people if p.id == leaving_id)

    await users.add_away(session, leaving, MONDAY, MONDAY + timedelta(days=3), MONDAY)
    await session.commit()
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    ids = [pd.user_id for pd in kitchen.potential_drivers]
    assert len(ids) == 2 and leaving_id not in ids

    preview = await duties.preview(
        session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=3)
    )
    assert all(
        leaving_id not in e.potential_drivers for plan in preview.plans.values() for e in plan
    )


async def test_delete_user_keeps_history_and_replaces(session):
    people = await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    trash = await duties.get_trip(session, Duty.trash, MONDAY)
    gone_id = trash.potential_drivers[0].user_id
    gone = next(p for p in people if p.id == gone_id)
    await users.delete(session, gone, MONDAY)
    await session.commit()
    trash = await duties.get_trip(session, Duty.trash, MONDAY)
    assert [pd.user_id for pd in trash.potential_drivers] != [gone_id]
    assert len(trash.potential_drivers) == 1


async def test_month_of_ticks_is_fair(session):
    """30 days, one random person of each pair cleans: everybody cleans 2 times."""
    rng = random.Random(7)
    await make_users(session)
    day = MONDAY
    for _ in range(30):
        await jobs.daily_tick(session, at(day))
        trip = await duties.get_trip(session, Duty.kitchen, day)
        driver = await duties.get_potential_driver(session, rng.choice(trip.potential_drivers).id)
        await duties.mark_as_driver(session, driver, driver.user, at(day, 20))
        await session.commit()
        day += timedelta(days=1)
    rows = (
        (
            await session.execute(
                select(PotentialDriver.user_id)
                .join(PotentialDriver.trip)
                .where(Trip.duty == Duty.kitchen, PotentialDriver.status == Status.drove)
            )
        )
        .scalars()
        .all()
    )
    counts = [rows.count(u) for u in set(rows)]
    assert len(set(rows)) == 15 and max(counts) - min(counts) <= 1


@pytest.fixture
async def client(session_factory):
    """HTTP client that calls the app directly (no server), keeping the login cookie."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def login(client, username):
    """Log in as `username` with the test password."""
    r = await client.post("/login", data={"username": username, "password": "secret123"})
    assert r.status_code == 303


async def test_web_permissions(session, client):
    await make_users(session, n=3)
    r = await client.get("/me")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = await client.get("/")
    assert r.status_code == 200 and "Kitchen duty" in r.text

    await login(client, "p2")
    assert (await client.get("/admin")).status_code == 403
    assert (await client.get("/me")).status_code == 200

    await client.post("/logout")
    await login(client, "p1")
    assert (await client.get("/admin")).status_code == 200
    r = await client.post(
        "/admin/users",
        data={"name": "Neu", "room_number": "E120", "username": "neu", "password": "pw123456"},
    )
    assert r.status_code == 303
    assert "Neu" in (await client.get("/admin")).text


def assert_no_personal_data(text: str, people) -> None:
    """None of the residents' names, usernames or rooms appear in `text`."""
    for p in people:
        for value in (p.name, p.first_name, p.room_number, p.username):
            assert value not in text, f"{value!r} visible without login"


def assert_sent_to_login(r: httpx.Response, path: str, people) -> None:
    """`r` redirects to the login page (coming back to `path`) and shows nothing personal."""
    assert r.status_code == 303, f"{r.request.method} {path}: {r.status_code}, not a redirect"
    assert r.headers["location"] == f"/login?next={path}"
    assert_no_personal_data(r.text, people)


async def test_session_of_a_deleted_user_is_no_login(session, client):
    people = await make_users(session, n=4)
    await login(client, "p3")
    assert (await client.get("/me")).status_code == 200
    await users.delete(session, people[2], settings.today())
    await session.commit()
    assert_sent_to_login(await client.get("/me"), "/me", people[:2] + people[3:])
    # The stale session was cleared.
    assert "a4_session" not in client.cookies


async def test_logged_out_session_is_no_login(session, client):
    people = await make_users(session, n=4)
    await login(client, "p2")
    assert (await client.get("/me")).status_code == 200
    await client.post("/logout")
    assert_sent_to_login(await client.get("/me"), "/me", people)


def session_payload(data: dict) -> bytes:
    """Session data the way Starlette's `SessionMiddleware` puts it into the cookie."""
    return base64.b64encode(json.dumps(data).encode())


def set_session_cookie(client, value: str) -> None:
    """Replace the session cookie (not add a second one next to it)."""
    client.cookies.clear()
    client.cookies.set("a4_session", value)


async def test_forged_session_cookie_is_no_login(session, client):
    people = await make_users(session, n=4)
    admin = people[0]

    def signed_with(key: str) -> str:
        middleware = SessionMiddleware(app=None, secret_key=key)
        return middleware.signer.sign(session_payload({"user_id": admin.id})).decode()

    # Control: built like this with the app's key, the cookie is a login. So the cases
    # below fail because of the key or the signature, not because the cookie is malformed.
    set_session_cookie(client, signed_with(settings.secret_key))
    assert (await client.get("/admin")).status_code == 200

    # Signed with another key.
    set_session_cookie(client, signed_with("x" * 32))
    assert_sent_to_login(await client.get("/admin"), "/admin", people)

    # A real login with the user id swapped for the floor speaker's: the signature no
    # longer matches.
    client.cookies.clear()
    await login(client, "p2")
    _, signature = client.cookies["a4_session"].split(".", 1)
    set_session_cookie(client, f"{session_payload({'user_id': admin.id}).decode()}.{signature}")
    assert_sent_to_login(await client.get("/admin"), "/admin", people)


async def test_help_page(session, client):
    await make_users(session, n=3)
    r = await client.get("/help")
    assert r.status_code == 200 and "Kitchen duty" in r.text
    assert "For the floor speaker" not in r.text

    await login(client, "p2")
    assert "For the floor speaker" not in (await client.get("/help")).text
    await client.post("/logout")
    await login(client, "p1")
    assert "For the floor speaker" in (await client.get("/help")).text


async def test_understaffed_trip_is_topped_up(session):
    # The tick ran before anyone lived on the floor.
    await jobs.daily_tick(session, at(MONDAY))
    assert (await duties.get_trip(session, Duty.kitchen, MONDAY)).potential_drivers == []

    await make_users(session, n=5)
    await jobs.daily_tick(session, at(MONDAY, 10))
    assert len((await duties.get_trip(session, Duty.kitchen, MONDAY)).potential_drivers) == 2
    assert len((await duties.get_trip(session, Duty.trash, MONDAY)).potential_drivers) == 1


async def test_history_page_and_dispute(session, client):
    people = await make_users(session, n=4)
    _, first, _, p1, _ = await kitchen_pair(session, people, settings.today())
    await duties.mark_as_driver(session, first, p1, settings.now())
    await session.commit()

    assert (await client.get("/history")).status_code == 303  # login required
    await login(client, "p2")
    r = await client.get("/history")
    assert r.status_code == 200 and str(person_html(p1)) in r.text
    assert ">Dispute</button>" not in r.text
    assert (await client.post(f"/admin/potential-drivers/{first.id}/dispute")).status_code == 403

    await client.post("/logout")
    await login(client, "p1")
    r = await client.get("/history")
    assert ">Dispute</button>" in r.text
    r = await client.post(f"/admin/potential-drivers/{first.id}/dispute?next=/history")
    assert r.status_code == 303
    assert ">disputed<" in (await client.get("/history")).text


async def test_top_up_skips_trip_that_is_already_driven(session):
    # Only one resident: they get today's kitchen duty alone and do it.
    [first] = await make_users(session, n=1)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    a = await duties.get_potential_driver(session, kitchen.potential_drivers[0].id)
    await duties.mark_as_driver(session, a, first, at(MONDAY, 10))
    await session.commit()

    # A resident added later must not be put on the driven trip (and struck for it).
    newbie = await users.create(
        session, name="Neu", room_number="E120", username="neu", password="secret123"
    )
    await duties.fill_current(session, MONDAY)
    await session.commit()
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    assert [x.user_id for x in kitchen.potential_drivers] == [first.id]

    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), 13))
    assert await strikes.active_strikes(session, newbie.id) == []


async def test_web_errors_do_not_break_the_page(session, client):
    people = await make_users(session, n=4)
    _, first, _, p1, _ = await kitchen_pair(session, people, settings.today())
    outsider = next(p for p in people if p.id != p1.id and not p.is_admin)

    # Clicking someone else's card button reloads the page instead of nesting it in the card.
    await login(client, outsider.username)
    r = await client.post(
        f"/potential-drivers/{first.id}/drove?card=1", headers={"HX-Request": "true"}
    )
    assert r.status_code == 200 and r.headers["HX-Refresh"] == "true"

    await client.post("/logout")
    await login(client, "p1")
    r = await client.post("/admin/potential-drivers/999999/excuse")
    assert r.status_code == 303 and r.headers["location"] == "/admin"


async def test_login_is_blocked_after_too_many_failures(session, client, monkeypatch):
    monkeypatch.setattr("app.web.login_throttle", LoginThrottle(max_failures=3))
    await make_users(session, n=2)
    for _ in range(3):
        r = await client.post("/login", data={"username": "p2", "password": "wrong"})
        assert "Wrong username or password" in (await client.get(r.headers["location"])).text

    # Even the right password is refused while blocked.
    r = await client.post("/login", data={"username": "p2", "password": "secret123"})
    assert "Too many failed attempts" in (await client.get(r.headers["location"])).text
    assert (await client.get("/me")).status_code == 303
    # Other users can still log in.
    await login(client, "p1")
    assert (await client.get("/me")).status_code == 200


async def test_concurrent_logins_cannot_get_past_the_limit(session, client, monkeypatch):
    """Many guesses at once: each counts before its password check, not after it."""
    monkeypatch.setattr("app.web.login_throttle", LoginThrottle(max_failures=3))
    await make_users(session, n=2)
    checked = 0
    authenticate = web.auth.authenticate

    async def counting_authenticate(*args):
        nonlocal checked
        checked += 1
        await asyncio.sleep(0.01)  # like the real check, let the other requests run meanwhile
        return await authenticate(*args)

    monkeypatch.setattr(web.auth, "authenticate", counting_authenticate)
    data = {"username": "p2", "password": "wrong"}
    await asyncio.gather(*(client.post("/login", data=data) for _ in range(20)))
    assert checked == 3


async def test_guessing_the_old_password_is_limited(session, client, monkeypatch):
    """Someone with a session (an unlocked phone) can't find out the password by trying."""
    monkeypatch.setattr("app.web.login_throttle", LoginThrottle(max_failures=3))
    await make_users(session, n=2)
    await login(client, "p2")

    async def change(old: str) -> str:
        data = {"old_password": old, "new_password": "new-secret-1"}
        r = await client.post("/me/password", data=data)
        return (await client.get(r.headers["location"])).text

    for _ in range(3):
        assert "Old password is wrong" in await change("wrong")
    # Even the right one is refused while blocked.
    assert "Too many failed attempts" in await change("secret123")
    await client.post("/logout")
    await login(client, "p2")  # the password didn't change


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "http://test:8080"},  # another port is another site
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},  # e.g. another app on a sibling subdomain
    ],
)
async def test_posts_from_other_sites_are_refused(session, client, headers):
    """A page elsewhere can't make a logged-in floor speaker's browser change anything."""
    people = await make_users(session, n=3)
    await login(client, "p1")
    target = people[2]
    r = await client.post(
        f"/admin/users/{target.id}/edit",
        data={"name": "Hacked", "room_number": target.room_number, "is_admin": "true"},
        headers=headers,
    )
    assert r.status_code == 403
    await session.refresh(target)
    assert target.name == "Person 3" and not target.is_admin
    # Logging in from another site (login CSRF) is refused too.
    r = await client.post(
        "/login", data={"username": "p2", "password": "secret123"}, headers=headers
    )
    assert r.status_code == 403


async def test_posts_from_this_site_are_accepted(session, client):
    people = await make_users(session, n=3)
    await login(client, "p1")
    for headers in (
        {"Origin": "http://test", "Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},  # typed into the address bar / a bookmark
        {},  # old browsers and scripts send neither
    ):
        r = await client.post(
            f"/admin/users/{people[2].id}/edit",
            data={"name": "Renamed", "room_number": "E103"},
            headers=headers,
        )
        assert r.status_code == 303 and r.headers["location"] == "/admin", headers


@pytest.mark.parametrize(
    "url", ["//evil.com", "/\\evil.com", "/\t/evil.com", "\\\\evil.com", "/a\r\nX: y", "http://x"]
)
def test_next_never_leaves_the_site(url):
    assert web.safe_next(url) == "/"


def test_next_keeps_paths_on_this_site():
    assert web.safe_next("/history?until=2026-11-01") == "/history?until=2026-11-01"


async def test_short_passwords_are_rejected(session):
    with pytest.raises(UserError):
        await users.create(session, name="A", room_number="E101", username="a", password="short")


def test_room_numbers():
    assert users.check_room(" e103 ") == "E103"
    for valid in ["E101", "E199", "B110"]:
        assert users.check_room(valid) == valid
    for invalid in ["E10", "E100", "E.121", "E1.1", "E1100", "1103", "EE103", ""]:
        with pytest.raises(UserError):
            users.check_room(invalid)


async def test_initial_admin_only_on_empty_database(session, monkeypatch):
    monkeypatch.setattr(settings, "admin_username", "")  # whatever the local .env says
    assert await users.create_initial_admin(session) is None
    for key, value in dict(username="Chef", password="secret123", name="Chef", room="E101").items():
        monkeypatch.setattr(settings, f"admin_{key}", value)
    admin = await users.create_initial_admin(session)
    assert admin.is_admin and admin.username == "chef"
    await session.commit()
    # Not again once there are users, e.g. after the admin was deleted and others were added.
    assert await users.create_initial_admin(session) is None


def test_login_throttle_window():
    now = [0.0]
    throttle = LoginThrottle(max_failures=2, window_seconds=60, clock=lambda: now[0])
    throttle.record_failure("k")
    throttle.record_failure("k")
    assert throttle.is_blocked("k")
    now[0] = 61
    assert not throttle.is_blocked("k")
    throttle.record_failure("k")
    throttle.reset("k")
    assert not throttle.is_blocked("k")


async def test_moving_out_does_not_change_the_others_balance(session):
    people = await make_users(session, n=5)
    day = MONDAY
    for _ in range(10):
        await jobs.daily_tick(session, at(day))
        trip = await duties.get_trip(session, Duty.kitchen, day)
        driver = await duties.get_potential_driver(session, trip.potential_drivers[0].id)
        await duties.mark_as_driver(session, driver, driver.user, at(day, 20))
        await session.commit()
        day += timedelta(days=1)

    leaving, staying = people[1], [p for p in people if p is not people[1]]
    staying_ids = [p.id for p in staying]
    before = await duties.load_balances(session, Duty.kitchen)
    # Move out on the last scheduled day, so no new trip changes the balances.
    await users.delete(session, leaving, day - timedelta(days=1))
    await session.commit()
    after = await duties.load_balances(session, Duty.kitchen)
    assert [after[uid].score for uid in staying_ids] == [before[uid].score for uid in staying_ids]

    # The new resident of that room starts with a clean slate.
    newbie = await users.create(
        session, name="Neu", room_number=leaving.room_number, username="neu", password="secret123"
    )
    await session.commit()
    balances = await duties.load_balances(session, Duty.kitchen)
    assert newbie.id not in balances
    assert await strikes.active_strikes(session, newbie.id) == []


async def test_moving_out_drops_open_penalty_week(session):
    people = await make_users(session)
    leaving = people[1]
    await jobs.daily_tick(session, at(MONDAY))
    for _ in range(3):
        await strikes.issue(session, user_id=leaving.id, reason="x", today=MONDAY)
    await session.commit()

    await users.delete(session, leaving, MONDAY)
    await session.commit()
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=7)))
    trip = await duties.get_trip(session, Duty.trash, MONDAY + timedelta(days=7))
    assert not trip.is_penalty and len(trip.potential_drivers) == 1


async def test_own_name_is_highlighted(session, client):
    people = await make_users(session, n=3)
    await login(client, "p2")
    text = (await client.get("/stats")).text
    assert str(person_html(people[1], is_me=True)) in text
    assert str(person_html(people[0])) in text


async def test_stats_shows_whole_points(session, client):
    """Scores 2/3, -1/3, -1/3 are shown as whole points that still sum to 0."""
    people = await make_users(session, n=3)
    _, first, _, p1, _ = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))
    await session.commit()
    text = (await client.get("/stats")).text
    cells = re.findall(r'<td class="points">([^<]*)</td>', text)
    kitchen = [int(c.replace("\u2212", "-")) for c in cells[::2]]
    assert sorted(kitchen) == [-34, -33, 67]
    assert all(c == "0" for c in cells[1::2])


async def test_dashboard_switches_between_plans(session, client):
    people = await make_users(session, n=6)
    _, _, _, p1, p2 = await kitchen_pair(session, people, settings.today())
    await session.commit()

    def current(text):
        return re.findall(r'cleaner=(\d+)"[^>]*\n?[^>]*aria-current="true"', text)

    text = (await client.get("/")).text
    assert "The plan depends on who cleans today" in text
    assert current(text) == [str(p1.id)]
    text = (await client.get(f"/?cleaner={p2.id}")).text
    assert current(text) == [str(p2.id)]
    # Unknown ids fall back to the default; logged in as one of the pair, that's yourself.
    await login(client, p2.username)
    assert current((await client.get("/?cleaner=999")).text) == [str(p2.id)]
    assert "If you clean" in (await client.get("/")).text
    # Anything that isn't an id falls back too, instead of an error page.
    r = await client.get("/?cleaner=abc")
    assert r.status_code == 200 and current(r.text) == [str(p2.id)]


async def test_switching_plans_shows_different_tables(session, client):
    people = await make_users(session, n=6)
    _, _, _, p1, p2 = await kitchen_pair(session, people, settings.today())
    await session.commit()

    def kitchen_preview(text):
        start = text.index('id="kitchen-preview"')
        return text[start : text.index("</section>", start)]

    headers = {"HX-Request": "true"}
    one = (await client.get(f"/?cleaner={p1.id}", headers=headers)).text
    two = (await client.get(f"/?cleaner={p2.id}", headers=headers)).text
    # The tables differ (the switch itself aside): whoever doesn't clean is up tomorrow.
    assert (
        kitchen_preview(one).split("</table>")[0].split("<table>")[1]
        != (kitchen_preview(two).split("</table>")[0].split("<table>")[1])
    )


async def test_done_on_the_card_updates_card_and_preview(session, client):
    people = await make_users(session, n=6)
    _, first, _, p1, _ = await kitchen_pair(session, people, settings.today())
    await session.commit()
    await login(client, p1.username)
    r = await client.post(
        f"/potential-drivers/{first.id}/drove?card=1",
        headers={"HX-Request": "true"},
        follow_redirects=True,
    )
    # htmx follows to the dashboard and takes the card and the previews from it.
    assert r.status_code == 200 and 'id="card-kitchen"' in r.text
    assert 'id="kitchen-preview"' in r.text and "The plan depends on" not in r.text


async def test_preview_with_one_driver_left_has_no_switch(session):
    people = await make_users(session)
    _, first, _, _, p2 = await kitchen_pair(session, people)
    admin = people[0]
    await duties.excuse(session, first, admin, at(MONDAY, 10))
    preview = await duties.preview(
        session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=7)
    )
    assert list(preview.plans) == [p2.id]


async def test_dashboard_without_preview_days(session, client, monkeypatch):
    people = await make_users(session, n=4)
    await kitchen_pair(session, people, settings.today())
    await session.commit()
    monkeypatch.setattr(settings, "preview_days", 0)
    assert (await client.get("/")).status_code == 200


async def test_stats_colour_strikes(session, client):
    people = await make_users(session, n=3)
    await strikes.issue(session, user_id=people[1].id, reason="x", today=MONDAY)
    for _ in range(2):
        await strikes.issue(session, user_id=people[2].id, reason="x", today=MONDAY)
    await session.commit()
    text = (await client.get("/stats")).text
    assert '"strikes ok">0 / 3' in text
    assert '"strikes warn">1 / 3' in text
    assert '"strikes bad">2 / 3' in text
