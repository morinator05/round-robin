"""Rules against a real database: assigning, deadlines, strikes, moving out, web pages."""

import asyncio
import base64
import html
import json
import random
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from fractions import Fraction

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Route

from app import web
from app.config import settings
from app.formatting import long_date, short_date
from app.main import app
from app.models import (
    AwayPeriod,
    DishwasherClear,
    Duty,
    PotentialDriver,
    Status,
    Strike,
    Trip,
    User,
)
from app.services import auth, carpool, dishwasher, duties, jobs, strikes, users
from app.services.dishwasher import DishwasherError
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
    # `get_potential_driver` locks the trip's rows until the transaction ends; requests
    # through the app (another session) would wait for them.
    await session.commit()
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
            session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=30), MONDAY
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


async def test_undo_reopens_the_trip_for_everyone(session):
    people = await make_users(session)
    _, first, second, p1, p2 = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))

    await duties.undo_driver(session, first, p1, at(MONDAY, 20) + timedelta(minutes=9))
    assert first.status is second.status is Status.pending
    assert first.completed_at is None and first.completed_by_id is None
    # Open again: either of them can mark it, and the preview has a plan per person again.
    assert duties.can_mark_as_driver(second, p2, at(MONDAY, 21))
    preview = await duties.preview(
        session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=14), MONDAY
    )
    assert set(preview.plans) == {p1.id, p2.id}


async def test_undo_is_only_for_a_short_while_and_for_whoever_marked_it(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, _, p1, p2 = await kitchen_pair(session, people)
    marked = at(MONDAY, 20)
    await duties.mark_as_driver(session, first, p1, marked)

    late = marked + timedelta(minutes=duties.UNDO_MINUTES + 1)
    assert not duties.can_undo(first, p1, late)
    with pytest.raises(DutyError):
        await duties.undo_driver(session, first, p1, late)
    # Not the partner, who didn't mark it; the floor speaker may.
    assert not duties.can_undo(first, p2, marked + timedelta(minutes=1))
    assert duties.can_undo(first, admin, marked + timedelta(minutes=1))


async def test_no_undo_after_the_deadline(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, _, p1, _ = await kitchen_pair(session, people)
    deadline = duties.deadline(first.trip)
    await duties.mark_as_driver(session, first, p1, deadline - timedelta(minutes=2))
    assert not duties.can_undo(first, p1, deadline)
    assert not duties.can_undo(first, admin, deadline + timedelta(minutes=1))


async def test_no_undo_of_a_disputed_trip(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, second, p1, p2 = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))
    await duties.dispute(session, first, admin, at(MONDAY, 20) + timedelta(minutes=1))
    # The admin marks the partner as the one who cleaned; that can't be undone (a strike is
    # involved on this trip).
    await duties.mark_as_driver(session, second, admin, at(MONDAY, 20) + timedelta(minutes=2))
    assert not duties.can_undo(second, admin, at(MONDAY, 20) + timedelta(minutes=3))


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
            await users.delete(session, people[9], at(day, 12))
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
    before = await duties.preview(
        session, Duty.kitchen, tomorrow, today + timedelta(days=14), today
    )
    assert set(before.plans) == {pd.user_id for pd in pair}

    driver = pair[which]
    pd = await duties.get_potential_driver(session, driver.id)
    await duties.mark_as_driver(session, pd, by_id[driver.user_id], at(today, 20))
    await session.commit()
    after = await duties.preview(session, Duty.kitchen, tomorrow, today + timedelta(days=14), today)
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

    preview = await duties.preview(
        session, Duty.kitchen, tuesday, MONDAY + timedelta(days=14), MONDAY
    )
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
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    on_duty = {pd.user_id for pd in kitchen.potential_drivers}
    await login(client, next(p for p in people if p.id not in on_duty).username)
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


async def test_away_keeps_assigned_duty_but_is_not_chosen_again(session):
    """An absence doesn't take you off a duty you already have (the floor speaker can
    excuse it), but you aren't chosen during it."""
    people = await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    away_id = kitchen.potential_drivers[0].user_id
    before = [pd.user_id for pd in kitchen.potential_drivers]
    away = next(p for p in people if p.id == away_id)

    await users.add_away(session, away, MONDAY, MONDAY + timedelta(days=3), MONDAY)
    await session.commit()
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    assert [pd.user_id for pd in kitchen.potential_drivers] == before
    await session.refresh(kitchen, ["riders"])
    assert away_id in {r.user_id for r in kitchen.riders}

    preview = await duties.preview(
        session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=3), MONDAY
    )
    assert all(away_id not in e.potential_drivers for plan in preview.plans.values() for e in plan)


async def test_pausing_replaces_pending_potential_driver(session):
    people = await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    paused_id = kitchen.potential_drivers[0].user_id
    paused = next(p for p in people if p.id == paused_id)

    await users.set_active(session, paused, False, at(MONDAY, 12))
    await session.commit()
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    ids = [pd.user_id for pd in kitchen.potential_drivers]
    assert len(ids) == 2 and paused_id not in ids


async def test_away_over_own_duty_says_it_stays(session, client):
    people = await make_users(session, n=4)
    _, first, _, p1, _ = await kitchen_pair(session, people, settings.today())
    await login(client, p1.username)
    today = settings.today().isoformat()
    await client.post("/me/away", data={"start_date": today, "end_date": today})
    text = (await client.get("/me")).text
    assert "Away period added" in text and "stays with you" in text


async def test_delete_user_keeps_history_and_replaces(session):
    people = await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    trash = await duties.get_trip(session, Duty.trash, MONDAY)
    gone_id = trash.potential_drivers[0].user_id
    gone = next(p for p in people if p.id == gone_id)
    await users.delete(session, gone, at(MONDAY, 12))
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
    await login_with(client, username, "secret123")


async def login_with(client, username, password):
    """Log in as `username`; fails the test if that doesn't work."""
    r = await client.post("/login", data={"username": username, "password": password})
    assert r.status_code == 303 and r.headers["location"] == "/", f"login as {username} failed"


async def test_web_permissions(session, client):
    await make_users(session, n=3)
    r = await client.get("/me")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")

    await login(client, "p2")
    r = await client.get("/")
    assert r.status_code == 200 and "Kitchen duty" in r.text
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


# Reachable without login. Everything else must send you to the login page.
PUBLIC_ROUTES = {("GET", "/login"), ("POST", "/login"), ("POST", "/logout")}


def assert_no_personal_data(text: str, people) -> None:
    """None of the residents' names, usernames or rooms appear in `text`."""
    for p in people:
        for value in (p.name, p.first_name, p.room_number, p.username):
            assert value not in text, f"{value!r} visible without login"


def all_routes(routes):
    """`routes` with included routers expanded: FastAPI keeps those as one wrapper each."""
    for route in routes:
        if (sub := getattr(route, "original_router", None)) is not None:
            yield from all_routes(sub.routes)
        else:
            yield route


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


async def test_every_page_needs_a_login(session, client):
    """Every route except the login itself redirects to the login page without a session.

    Goes through `app.routes`, so routes added later are checked too. A route with a path
    parameter this test doesn't know yet fails here: add an id for it to `ids`. The POSTs
    also must not change anything.
    """
    people = await make_users(session, n=4)
    _, first, _, _, _ = await kitchen_pair(session, people, settings.today())
    later = settings.today() + timedelta(days=30)
    await users.add_away(session, people[1], later, later + timedelta(days=1), settings.today())
    away = await session.scalar(select(AwayPeriod))
    strike = await strikes.issue(session, user_id=people[2].id, reason="x", today=MONDAY)
    clear = await dishwasher.mark_cleared(session, people[3], settings.now())
    await session.commit()
    ids = {
        "user_id": people[1].id,
        "potential_driver_id": first.id,
        "away_id": away.id,
        "strike_id": strike.id,
        "clear_id": clear.id,
    }

    before = await row_counts(session)

    checked = set()
    for route in all_routes(app.routes):
        if not isinstance(route, Route):  # e.g. the /static mount
            continue
        unknown = set(route.param_convertors) - set(ids)
        assert not unknown, f"{route.path}: add an id for {unknown} to this test"
        path = route.path.format(**ids)
        for method in sorted(route.methods - {"HEAD"}):
            if (method, route.path) in PUBLIC_ROUTES:
                continue
            for headers in ({}, {"HX-Request": "true"}):
                r = await client.request(method, path, headers=headers)
                assert_sent_to_login(r, path, people)
            checked.add((method, route.path))
    # All of the app's own routes were reached, not just FastAPI's.
    own = {
        (method, route.path) for route in web.router.routes for method in route.methods - {"HEAD"}
    }
    assert own - PUBLIC_ROUTES <= checked
    # The pages that used to be public are among them.
    assert {("GET", "/"), ("GET", "/stats"), ("GET", "/help")} <= checked
    session.expire_all()
    assert await row_counts(session) == before


async def test_pages_with_query_strings_need_a_login(session, client):
    people = await make_users(session, n=4)
    await kitchen_pair(session, people, settings.today())
    for url in (f"/?cleaner={people[1].id}", "/history?until=2026-11-01"):
        r = await client.get(url)
        # After logging in the same page comes back, query string included.
        query = url.split("?")[1].replace("=", "%3D")
        assert_sent_to_login(r, f"{url.split('?')[0]}%3F{query}", people)


async def test_api_docs_are_off(session, client):
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert (await client.get(path)).status_code == 404, path


def page_links(text: str) -> set[str]:
    """Where the page's links point to, without stylesheets and fonts."""
    return {h for h in re.findall(r'href="([^"]*)"', text) if not h.startswith("/static/")}


async def test_login_page_shows_nothing_personal(session, client, monkeypatch):
    monkeypatch.setattr("app.web.login_throttle", LoginThrottle())
    people = await make_users(session, n=4)
    await kitchen_pair(session, people, settings.today())
    r = await client.get("/login")
    assert r.status_code == 200 and 'name="password"' in r.text
    assert_no_personal_data(r.text, people)
    # No links to the other pages either, they'd only lead back here. Just the brand ("/").
    assert page_links(r.text) <= {"/", "/login"}

    # Not after a failed login either.
    r = await client.post("/login", data={"username": "p2", "password": "wrong"})
    r = await client.get(r.headers["location"])
    assert "Wrong username or password" in r.text
    assert_no_personal_data(r.text, people)


async def test_pages_are_not_kept_after_logout(session, client):
    """After logging out, Back must not show personal pages from the browser's cache.

    htmx keeps the last pages in localStorage (for Back) unless told not to, and browsers
    keep pages in their cache unless the response says `no-store`.
    """
    await make_users(session, n=3)
    await login(client, "p1")
    for path in ("/", "/stats", "/history", "/me", "/admin", "/help"):
        r = await client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store", path
        assert '"historyCacheSize":0' in r.text and '"refreshOnHistoryMiss":true' in r.text
    # Redirects and the login page too, they can carry flash messages.
    assert (await client.post("/logout")).headers["cache-control"] == "no-store"
    assert (await client.get("/login")).headers["cache-control"] == "no-store"
    # Static files have nothing personal and may be cached.
    assert "no-store" not in (await client.get("/static/style.css")).headers.get(
        "cache-control", ""
    )


async def test_static_files_are_public(session, client):
    # The login page needs them.
    assert (await client.get("/static/style.css")).status_code == 200
    assert (await client.get("/static/htmx.min.js")).status_code == 200


async def test_session_of_a_deleted_user_is_no_login(session, client):
    people = await make_users(session, n=4)
    await login(client, "p3")
    assert (await client.get("/me")).status_code == 200
    await users.delete(session, people[2], settings.now())
    await session.commit()
    assert_sent_to_login(await client.get("/me"), "/me", people[:2] + people[3:])
    # The stale session was cleared.
    assert "rr_session" not in client.cookies


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
    client.cookies.set("rr_session", value)


async def test_forged_session_cookie_is_no_login(session, client):
    people = await make_users(session, n=4)
    admin = people[0]

    def signed_with(key: str) -> str:
        data = {"user_id": admin.id, "token": admin.session_token}
        signer = SessionMiddleware(app=None, secret_key=key).signer
        return signer.sign(session_payload(data)).decode()

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
    _, signature = client.cookies["rr_session"].split(".", 1)
    set_session_cookie(client, f"{session_payload({'user_id': admin.id}).decode()}.{signature}")
    assert_sent_to_login(await client.get("/admin"), "/admin", people)


def session_cookie_for(data: dict) -> str:
    """A session cookie with `data`, validly signed with the app's key."""
    signer = SessionMiddleware(app=None, secret_key=settings.secret_key).signer
    return signer.sign(session_payload(data)).decode()


@asynccontextmanager
async def device():
    """Another browser (phone, laptop) with its own cookies, on the same app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def is_logged_in(client) -> bool:
    r = await client.get("/me")
    if r.status_code == 303:
        assert r.headers["location"].startswith("/login"), r.headers["location"]
        return False
    assert r.status_code == 200
    return True


async def test_cookie_without_a_session_token_is_no_login(session, client):
    """Cookies from before session tokens (only a user id) no longer log anyone in."""
    people = await make_users(session, n=3)
    set_session_cookie(client, session_cookie_for({"user_id": people[0].id}))
    assert_sent_to_login(await client.get("/admin"), "/admin", people)
    # Control: with the token it is a login.
    data = {"user_id": people[0].id, "token": people[0].session_token}
    set_session_cookie(client, session_cookie_for(data))
    assert (await client.get("/admin")).status_code == 200


async def test_session_from_an_earlier_database_is_no_login(session, client):
    """After a reset, ids start over: an old cookie must not log you in as whoever has
    that id now."""
    people = await make_users(session, n=3)
    await login(client, "p2")
    old_cookie = client.cookies["rr_session"]
    old_id = people[1].id
    await users.delete(session, people[1], settings.now())
    await session.commit()
    session.add(
        User(
            id=old_id,
            name="Someone Else",
            room_number="E120",
            username="else",
            password_hash=auth.hash_password("secret123"),
        )
    )
    await session.commit()
    assert_sent_to_login(await client.get("/me"), "/me", [people[0], people[2]])
    assert "rr_session" not in client.cookies

    # Logging out with the old cookie must not log out the new resident either.
    async with device() as theirs:
        await login(theirs, "else")
        set_session_cookie(client, old_cookie)
        await client.post("/logout")
        assert await is_logged_in(theirs)


async def test_logout_ends_the_session_on_every_device(session, client):
    await make_users(session, n=3)
    async with device() as phone, device() as copy, device() as other_user:
        await login(client, "p2")
        await login(phone, "p2")
        copy.cookies.set("rr_session", client.cookies["rr_session"])  # e.g. sniffed
        await login(other_user, "p3")
        assert all([await is_logged_in(c) for c in (client, phone, copy, other_user)])

        await client.post("/logout")
        assert not await is_logged_in(client)
        assert not await is_logged_in(phone)
        assert not await is_logged_in(copy)
        assert await is_logged_in(other_user)
        # Logging in again works, also on the other device.
        await login(phone, "p2")
        assert await is_logged_in(phone)


async def test_password_change_ends_the_other_sessions(session, client):
    await make_users(session, n=3)
    async with device() as phone:
        await login(client, "p2")
        await login(phone, "p2")
        r = await client.post(
            "/me/password", data={"old_password": "secret123", "new_password": "new-secret-1"}
        )
        assert "Password changed" in (await client.get(r.headers["location"])).text
        # Whoever changed it stays logged in, everywhere else it's over.
        assert await is_logged_in(client)
        assert not await is_logged_in(phone)

        # A rejected change (wrong old password, too short) ends nothing.
        await login_with(phone, "p2", "new-secret-1")
        for old, new in (("wrong", "other-secret-1"), ("new-secret-1", "short")):
            await client.post("/me/password", data={"old_password": old, "new_password": new})
        assert await is_logged_in(client) and await is_logged_in(phone)


async def test_password_set_by_the_floor_speaker_ends_the_residents_sessions(session, client):
    people = await make_users(session, n=3)
    p2 = people[1]
    async with device() as resident:
        await login(resident, "p2")
        await login(client, "p1")

        async def edit(user, name, password=""):
            data = {"name": name, "room_number": user.room_number, "password": password}
            r = await client.post(f"/admin/users/{user.id}/edit", data=data)
            assert r.status_code == 303 and r.headers["location"] == "/admin"
            saved = f"{name.split()[0]} ({user.room_number}) saved"
            assert saved in (await client.get("/admin")).text

        # Only a new password ends the sessions, not a new name.
        await edit(p2, "Renamed")
        assert await is_logged_in(resident)
        await edit(p2, "Renamed", "reset-pass-1")
        assert not await is_logged_in(resident)
        await login_with(resident, "p2", "reset-pass-1")
        assert await is_logged_in(resident)

        # Setting your own password as floor speaker: this browser stays logged in, your
        # other devices don't.
        async with device() as admin_phone:
            await login(admin_phone, "p1")
            await edit(people[0], "Person 1", "admin-pass-1")
            assert (await client.get("/admin")).status_code == 200
            assert not await is_logged_in(admin_phone)


async def test_help_page(session, client):
    await make_users(session, n=3)
    await login(client, "p2")
    r = await client.get("/help")
    assert r.status_code == 200 and "Kitchen duty" in r.text
    assert "For the floor speaker" not in r.text
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
    await duties.fill_current(session, at(MONDAY, 12))
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
    await users.delete(session, leaving, at(day - timedelta(days=1), 12))
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

    await users.delete(session, leaving, at(MONDAY, 12))
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
    await login(client, "p1")
    text = (await client.get("/stats")).text
    cells = re.findall(r'<td class="points">([^<]*)</td>', text)
    kitchen = [int(c.replace("\u2212", "-")) for c in cells[::2]]
    assert sorted(kitchen) == [-34, -33, 67]
    assert all(c == "0" for c in cells[1::2])


@pytest.fixture
def midday(monkeypatch):
    """The app's clock at noon today: after the deadline hour, so the dashboard shows
    today's trip (before it, yesterday's), whenever the tests run."""
    noon = at(settings.today(), 12)
    monkeypatch.setattr(type(settings), "now", lambda self: noon)


async def test_dashboard_switches_between_plans(session, client, midday):
    people = await make_users(session, n=6)
    _, _, _, p1, p2 = await kitchen_pair(session, people, settings.today())
    await session.commit()

    def current(text):
        return re.findall(r'cleaner=(\d+)"[^>]*\n?[^>]*aria-current="true"', text)

    # Not on the duty yourself, the first one's plan is the default.
    await login(client, next(p for p in people if p.id not in {p1.id, p2.id}).username)
    text = (await client.get("/")).text
    assert "The plan depends on who cleans today" in text
    assert current(text) == [str(p1.id)]
    text = (await client.get(f"/?cleaner={p2.id}")).text
    assert current(text) == [str(p2.id)]
    # Unknown ids fall back to the default; logged in as one of the pair, that's yourself.
    await client.post("/logout")
    await login(client, p2.username)
    assert current((await client.get("/?cleaner=999")).text) == [str(p2.id)]
    page = (await client.get("/")).text
    # The buttons only switch the preview; they don't report anything.
    assert "Plan if you clean" in page and "only changes the preview" in page
    # Anything that isn't an id falls back too, instead of an error page.
    r = await client.get("/?cleaner=abc")
    assert r.status_code == 200 and current(r.text) == [str(p2.id)]


async def test_switching_plans_shows_different_tables(session, client, midday):
    people = await make_users(session, n=6)
    _, _, _, p1, p2 = await kitchen_pair(session, people, settings.today())
    await session.commit()

    def kitchen_preview(text):
        start = text.index('id="kitchen-preview"')
        return text[start : text.index("</section>", start)]

    await login(client, "p3")
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


def you_line(page: str) -> str:
    """The "you" summary at the top of the dashboard, whitespace collapsed."""
    return " ".join(page.split('id="you"')[1].split("</p>")[0].split())


async def test_dashboard_says_what_you_have_to_do(session, client, monkeypatch):
    # Noon: before the deadline hour the dashboard would still show yesterday's duty.
    monkeypatch.setattr(type(settings), "now", lambda self: at(MONDAY, 12))
    people = await make_users(session, n=6)
    _, first, second, p1, p2 = await kitchen_pair(session, people, settings.today())
    trash = await duties.get_trip(session, Duty.trash, settings.today())
    busy = {p1.id, p2.id} | {pd.user_id for pd in trash.potential_drivers}
    outsider = next(p for p in people if p.id not in busy and not p.is_admin)

    await login(client, p1.username)
    page = (await client.get("/")).text
    line = you_line(page)
    assert "You're on kitchen duty today" in line
    # The card says what to do and by when, to the pair.
    assert "Only one of you needs to clean" in page and "taps “I cleaned it” before" in page
    # Your own button says what it means; it isn't the "done" status word.
    assert ">I cleaned it</button>" in page and ">Done</button>" not in page
    assert "New here?" not in line

    await client.post("/logout")
    await login(client, outsider.username)
    page = (await client.get("/")).text
    line = you_line(page)
    assert "You're free today" in line and "Next kitchen:" in line and "Next trash:" in line
    assert "strike" not in line
    # Someone who has never been on a duty gets pointed to the explanation.
    assert "New here?" in line and 'href="/help"' in line
    assert "Only one of them needs to clean" in page

    await strikes.issue(session, user_id=outsider.id, reason="test", today=settings.today())
    await session.commit()
    line = you_line((await client.get("/")).text)
    assert f"1 of {settings.strike_threshold} strikes" in line and 'href="/me"' in line
    # What happens at the limit, right there.
    assert f"at {settings.strike_threshold}: an extra trash week" in line

    # After cleaning: thanks, also when you're on trash duty this week as well.
    await client.post("/logout")
    await login(client, p1.username)
    mine = first if first.user_id == p1.id else second
    await client.post(f"/potential-drivers/{mine.id}/drove")
    assert "You did kitchen duty today – thanks!" in you_line((await client.get("/")).text)


async def test_dashboard_at_night_talks_about_yesterday_and_today(session, client, monkeypatch):
    """Before the deadline hour the card still shows yesterday's duty; the "you" line must
    say so plainly, and call the duty that starts later today "today"."""
    people = await make_users(session, n=6)
    _, first, _, p1, p2 = await kitchen_pair(session, people)
    tuesday = MONDAY + timedelta(days=1)
    monkeypatch.setattr(type(settings), "now", lambda self: at(tuesday, 3))

    await login(client, p1.username)
    line = you_line((await client.get("/")).text)
    assert "Your kitchen duty from yesterday is still open" in line and "by 08:00" in line
    assert "duty yesterday." not in line

    # Someone outside the pair who is up today (from the deadline hour on).
    preview = await duties.preview(
        session, Duty.kitchen, tuesday, tuesday + timedelta(days=14), tuesday
    )
    plan = next(iter(preview.plans.values()))
    up_today = next(
        p for p in people if p.id in plan[0].potential_drivers and p.id not in {p1.id, p2.id}
    )
    await client.post("/logout")
    await login(client, up_today.username)
    line = you_line((await client.get("/")).text)
    words = re.sub(r"<[^>]+>", "", line)
    assert f"Next kitchen: today from {settings.deadline_hour:02d}:05" in words
    # The earliest outcome first, and what it depends on; the other one after "otherwise".
    other = next(
        (e for e in list(preview.plans.values())[1] if up_today.id in e.potential_drivers), None
    )
    if other and other.start != tuesday:
        assert " cleans, otherwise " in words
    # The card says when today's duty comes.
    page = html.unescape(text(await client.get("/")))
    assert f"Today's kitchen duty is assigned at {settings.deadline_hour:02d}:05" in page


async def test_next_kitchen_says_with_whom(session, client, midday):
    people = await make_users(session, n=6)
    _, first, _, p1, _ = await kitchen_pair(session, people, settings.today())
    await duties.mark_as_driver(session, first, p1, settings.now())
    await session.commit()
    today = settings.today()
    preview = await duties.preview(
        session, Duty.kitchen, today + timedelta(days=1), today + timedelta(days=14), today
    )
    entry = preview.plans[None][0]
    me, partner = (next(p for p in people if p.id == uid) for uid in entry.potential_drivers)
    await login(client, me.username)
    words = re.sub(r"<[^>]+>", "", you_line((await client.get("/")).text))
    assert f"with Person {partner.room_number}" in words


async def test_undo_on_the_card(session, client, monkeypatch):
    monkeypatch.setattr(type(settings), "now", lambda self: at(MONDAY, 12))
    people = await make_users(session, n=6)
    _, first, second, p1, p2 = await kitchen_pair(session, people, settings.today())
    await login(client, p1.username)
    card = {"HX-Request": "true"}
    page = (
        await client.post(
            f"/potential-drivers/{first.id}/drove?card=1", headers=card, follow_redirects=True
        )
    ).text
    undo = f'hx-post="/potential-drivers/{first.id}/undo?card=1"'
    assert undo in page
    r = await client.post(
        f"/potential-drivers/{first.id}/undo?card=1", headers=card, follow_redirects=True
    )
    # Open again: the Done buttons are back and the plan depends on who cleans again.
    assert undo not in r.text and f"/potential-drivers/{first.id}/drove?card=1" in r.text
    assert "The plan depends on" in r.text

    # The partner sees no Undo for someone else's Done, and can't undo it.
    await client.post(f"/potential-drivers/{first.id}/drove?card=1", headers=card)
    await client.post("/logout")
    await login(client, p2.username)
    assert undo not in (await client.get("/")).text
    r = await client.post(f"/potential-drivers/{first.id}/undo?card=1", headers=card)
    assert r.headers["HX-Refresh"] == "true"


async def test_admin_can_undo_a_done_on_the_admin_page(session, client):
    people = await make_users(session, n=6)
    _, first, _, _, _ = await kitchen_pair(session, people, settings.today())
    await login(client, "p1")
    await client.post(f"/potential-drivers/{first.id}/drove?next=/admin")
    undo = f'action="/potential-drivers/{first.id}/undo?next=/admin'
    assert undo in (await client.get("/admin")).text
    r = await client.post(f"/potential-drivers/{first.id}/undo?next=/admin")
    assert r.status_code == 303
    page = (await client.get(r.headers["location"])).text
    assert "Undone: the duty is open again" in page and undo not in page


async def test_preview_with_one_driver_left_has_no_switch(session):
    people = await make_users(session)
    _, first, _, _, p2 = await kitchen_pair(session, people)
    admin = people[0]
    await duties.excuse(session, first, admin, at(MONDAY, 10))
    preview = await duties.preview(
        session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=7), MONDAY
    )
    assert list(preview.plans) == [p2.id]


async def test_dashboard_without_preview_days(session, client, monkeypatch):
    people = await make_users(session, n=4)
    await kitchen_pair(session, people, settings.today())
    await session.commit()
    monkeypatch.setattr(settings, "preview_days", 0)
    await login(client, "p1")
    assert (await client.get("/")).status_code == 200


async def test_stats_colour_strikes(session, client):
    people = await make_users(session, n=3)
    await strikes.issue(session, user_id=people[1].id, reason="x", today=MONDAY)
    for _ in range(2):
        await strikes.issue(session, user_id=people[2].id, reason="x", today=MONDAY)
    await session.commit()
    await login(client, "p1")
    text = (await client.get("/stats")).text
    assert '"strikes ok">0 / 3' in text
    assert '"strikes warn">1 / 3' in text
    assert '"strikes bad">2 / 3' in text


# --- Fixes from the review ----------------------------------------------------------


async def penalty_trips(session) -> list[Trip]:
    """All penalty weeks, with their potential drivers, as they are in the database now."""
    return list(
        await session.scalars(
            select(Trip)
            .where(Trip.is_penalty.is_(True))
            .options(selectinload(Trip.potential_drivers))
            .execution_options(populate_existing=True)
        )
    )


async def test_away_period_cannot_end_too_far_ahead(session):
    people = await make_users(session, n=4)
    far = MONDAY + timedelta(days=users.MAX_AWAY_DAYS_AHEAD + 1)
    with pytest.raises(UserError):
        await users.add_away(session, people[1], MONDAY, far, MONDAY)
    await users.add_away(session, people[1], MONDAY, far - timedelta(days=1), MONDAY)


async def test_penalty_week_search_is_bounded(session):
    """An endless away period (e.g. from before the limit) doesn't crash the daily job."""
    people = await make_users(session, n=4)
    target = people[1]
    session.add(AwayPeriod(user_id=target.id, start_date=MONDAY, end_date=date(9999, 12, 31)))
    await session.flush()
    for _ in range(settings.strike_threshold):
        await strikes.issue(session, user_id=target.id, reason="x", today=MONDAY)
    [trip] = await penalty_trips(session)
    assert trip.start_date == MONDAY + timedelta(days=7)


async def test_revoking_a_redeemed_strike_cancels_the_open_penalty_week(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    pd = kitchen.potential_drivers[0]
    target = next(p for p in people if p.id == pd.user_id)
    for _ in range(settings.strike_threshold - 1):
        await strikes.issue(session, user_id=target.id, reason="x", today=MONDAY)
    await session.commit()
    # A missed duty gives the last strike and a penalty week ...
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1)))
    assert len(await penalty_trips(session)) == 1
    assert await strikes.active_strikes(session, target.id) == []

    # ... and marking that duty done after all takes both back.
    pd = await duties.get_potential_driver(session, pd.id)
    await duties.mark_as_driver(session, pd, admin, at(MONDAY + timedelta(days=1), 12))
    await session.commit()
    assert await penalty_trips(session) == []
    assert len(await strikes.active_strikes(session, target.id)) == settings.strike_threshold - 1


async def test_admin_revoking_a_strike_cancels_the_open_penalty_week(session):
    people = await make_users(session, n=4)
    target = people[1]
    given = [
        await strikes.issue(session, user_id=target.id, reason="x", today=MONDAY)
        for _ in range(settings.strike_threshold)
    ]
    assert len(await penalty_trips(session)) == 1
    await strikes.revoke(session, given[0], at(MONDAY))
    assert await penalty_trips(session) == []
    assert len(await strikes.active_strikes(session, target.id)) == settings.strike_threshold - 1


async def test_disputed_driver_keeps_strike_when_partner_gets_the_credit(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, second, p1, p2 = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))
    await duties.dispute(session, first, admin, at(MONDAY, 21))
    await session.commit()

    second = await duties.get_potential_driver(session, second.id)
    await duties.mark_as_driver(session, second, admin, at(MONDAY, 22))
    first = await duties.get_potential_driver(session, first.id)
    assert second.status is Status.drove
    assert first.status is Status.missed and first.disputed_at is not None
    assert len(await strikes.active_strikes(session, p1.id)) == 1


async def test_excuse_only_open_or_missed_duties(session):
    people = await make_users(session)
    admin = people[0]
    _, first, _, p1, _ = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))
    with pytest.raises(DutyError):
        await duties.excuse(session, first, admin, at(MONDAY, 21))
    assert first.status is Status.drove


async def test_paused_and_away_riders_pay_no_share(session):
    """Someone who isn't a potential driver but becomes unavailable stops riding."""
    people = await make_users(session, n=5)
    await jobs.daily_tick(session, at(MONDAY))
    trash = await duties.get_trip(session, Duty.trash, MONDAY)
    trash_id, driver_id = trash.id, trash.potential_drivers[0].user_id
    paused, away = [p for p in people if p.id != driver_id][:2]
    paused_id, away_id = paused.id, away.id

    await users.set_active(session, paused, False, at(MONDAY + timedelta(days=1), 12))
    await users.add_away(
        session, away, MONDAY + timedelta(days=2), MONDAY + timedelta(days=4), MONDAY
    )
    await session.commit()
    trash = await session.scalar(
        select(Trip)
        .where(Trip.id == trash_id)
        .options(selectinload(Trip.riders))
        .execution_options(populate_existing=True)
    )
    riders = {r.user_id for r in trash.riders}
    assert paused_id not in riders and away_id not in riders and driver_id in riders


async def test_admin_action_before_the_deadline_hour_does_not_choose_today(session):
    people = await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    await session.commit()
    tuesday = MONDAY + timedelta(days=1)
    await users.create(
        session, name="New", room_number="E120", username="new", password="secret123"
    )
    await duties.fill_current(session, at(tuesday, settings.deadline_hour - 1))
    assert await duties.get_trip(session, Duty.kitchen, tuesday) is None
    assert len(people) > 0


async def test_penalty_week_moves_when_away_and_waits_while_paused(session):
    people = await make_users(session, n=4)
    target = people[1]
    for _ in range(settings.strike_threshold):
        await strikes.issue(session, user_id=target.id, reason="x", today=MONDAY)
    [trip] = await penalty_trips(session)
    week = trip.start_date

    # Away in that week: the penalty week moves to the next free week.
    await users.add_away(session, target, week, week + timedelta(days=2), MONDAY)
    [trip] = await penalty_trips(session)
    assert trip.start_date == week + timedelta(days=7)
    await session.refresh(target)  # `add_away` expired everything

    # Paused: no penalty week until they are back; the strikes count again.
    await users.set_active(session, target, False, at(MONDAY, 12))
    assert await penalty_trips(session) == []
    assert len(await strikes.active_strikes(session, target.id)) == settings.strike_threshold
    await users.set_active(session, target, True, at(MONDAY, 12))
    assert len(await penalty_trips(session)) == 1


async def test_bad_query_values_do_not_give_500(session, client):
    await make_users(session, n=4)
    await login(client, "p1")
    for url in (
        "/?cleaner=²",
        "/?cleaner=" + "9" * 5000,
        "/history?until=",
        "/history?until=0001-01-01",
    ):
        r = await client.get(url)
        assert r.status_code == 200, url


async def test_admin_rejects_too_long_names(session, client):
    people = await make_users(session, n=2)
    await login(client, "p1")
    data = {
        "name": "x" * 200,
        "room_number": "E150",
        "username": "longname",
        "password": "secret123",
    }
    r = await client.post("/admin/users", data=data)
    assert r.status_code == 303
    r = await client.post(
        f"/admin/users/{people[1].id}/edit",
        data={"name": "x" * 200, "room_number": people[1].room_number},
    )
    assert r.status_code == 303
    assert "at most" in (await client.get("/admin")).text
    user_id, name = people[1].id, people[1].name
    session.expire_all()
    assert (await session.get(User, user_id)).name == name


async def test_htmx_request_without_login_redirects_the_whole_page(session, client):
    r = await client.post("/potential-drivers/1/drove?card=1", headers={"HX-Request": "true"})
    assert r.headers["hx-redirect"] == "/login?next=/"


async def test_pages_send_security_headers(session, client):
    r = await client.get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_login_throttle_forgets_old_keys():
    now = [0.0]
    throttle = LoginThrottle(window_seconds=10, clock=lambda: now[0])
    for i in range(1500):
        throttle.record_failure(f"user{i}")
    now[0] = 100.0
    throttle.record_failure("fresh")
    assert len(throttle._failures) == 1


async def test_login_expires_after_the_maximum_lifetime(session, client, monkeypatch):
    from app import deps

    await make_users(session, n=2)
    now = [1_000_000.0]
    monkeypatch.setattr(deps, "clock", lambda: now[0])
    await login(client, "p2")
    assert (await client.get("/me")).status_code == 200
    now[0] += deps.SESSION_MAX_SECONDS - 60
    assert (await client.get("/me")).status_code == 200
    now[0] += 120
    assert (await client.get("/me")).status_code == 303


async def test_login_is_limited_per_account_across_ips(session, monkeypatch):
    monkeypatch.setattr("app.web.account_throttle", LoginThrottle(max_failures=4))
    await make_users(session, n=2)
    for i in range(4):
        # Each attempt from another address: the per-IP limit never kicks in.
        transport = httpx.ASGITransport(app=app, client=(f"10.0.0.{i}", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/login", data={"username": "p2", "password": "wrong"})
    transport = httpx.ASGITransport(app=app, client=("10.0.0.99", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/login", data={"username": "p2", "password": "secret123"})
        assert "Too many failed attempts" in (await c.get(r.headers["location"])).text


async def test_admin_enters_an_absence_for_a_resident(session, client):
    people = await make_users(session, n=4)
    resident = people[2]
    later = settings.today() + timedelta(days=5)
    form = {
        "user_id": str(resident.id),
        "start_date": later.isoformat(),
        "end_date": (later + timedelta(days=2)).isoformat(),
    }
    await login(client, "p1")
    page = (await client.get("/admin")).text
    assert 'action="/admin/aways"' in page
    r = await client.post("/admin/aways", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert f"Away period added for {resident.display_name}" in (await client.get("/admin")).text
    assert await session.scalar(select(AwayPeriod.user_id)) == resident.id

    # A wrong period comes back as a message.
    await client.post("/admin/aways", data={**form, "end_date": settings.today().isoformat()})
    assert "The end can't be before the start" in html.unescape((await client.get("/admin")).text)

    # Residents can't enter absences for others.
    await client.post("/logout")
    await login(client, "p2")
    assert (await client.post("/admin/aways", data=form)).status_code == 403


async def test_admin_page_has_the_frequent_tasks_first(session, client):
    people = await make_users(session, n=4)
    _, first, _, _, _ = await kitchen_pair(session, people, settings.today())
    await login(client, "p1")
    page = (await client.get("/admin")).text
    sections = ["duties", "strikes", "absences", "dishwasher", "residents"]
    positions = [page.index(f'<section class="card" id="{s}"') for s in sections]
    assert positions == sorted(positions)
    # A jump bar at the top, with what's waiting: today's kitchen pair and trash person.
    jump = page.split('class="jump"')[1].split("</div>")[0]
    # Not a <nav>: that would get the look of the main menu bar.
    assert '<nav class="jump"' not in page
    assert all(f'href="#{s}"' in jump for s in sections)
    assert "3 open" in jump
    assert "last 14 days</span>" in page and "and planned" not in page
    assert "Runs by itself every day at" in page


async def test_admin_sees_what_needs_attention(session, client, midday):
    people = await make_users(session, n=4)
    await login(client, "p1")
    # Nothing yet: says so.
    assert "Nothing needs attention." in (await client.get("/admin")).text

    _, first, second, _, _ = await kitchen_pair(session, people, settings.today())
    trash = await duties.get_trip(session, Duty.trash, settings.today())
    for _ in range(settings.strike_threshold - 1):
        await strikes.issue(session, user_id=people[3].id, reason="x", today=settings.today())
    await session.commit()
    page = (await client.get("/admin")).text
    block = page.split('id="attention"')[1].split("</section>")[0]
    # Today's open duties with their buttons, and who is one strike before a penalty week.
    for pd in [first, second, *trash.potential_drivers]:
        assert f"/admin/potential-drivers/{pd.id}/excuse" in block
        assert f"/potential-drivers/{pd.id}/drove?next=/admin" in block
    near = block.split("before a penalty week:")[1]
    assert f"{people[3].room_number}</span>" in near and 'href="#strikes">2 of 3' in near
    assert "Nothing needs attention." not in block
    jump = page.split('class="jump"')[1].split("</div>")[0]
    assert 'href="#attention"' in jump


async def test_give_strike_confirm_names_the_resident(session, client):
    await make_users(session, n=3)
    await login(client, "p1")
    page = (await client.get("/admin")).text
    form = page.split('action="/admin/strikes"')[1].split(">")[0]
    # The text is built from the chosen resident when the form is sent.
    assert "hx-on:htmx:confirm" in form and "hx-confirm" not in form.replace(
        "hx-on:htmx:confirm", ""
    )


async def test_admin_confirmations_name_the_person(session, client):
    people = await make_users(session, n=4)
    _, first, second, p1, p2 = await kitchen_pair(session, people, settings.today())
    await strikes.issue(session, user_id=people[3].id, reason="test", today=settings.today())
    await session.commit()
    await login(client, "p1")
    page = html.unescape((await client.get("/admin")).text)
    name = (p1 if first.user_id == p1.id else p2).display_name
    assert f"hx-confirm=\"Excuse {name}'s kitchen duty on" in page
    assert f"hx-confirm=\"Revoke {people[3].display_name}'s strike from" in page
    assert f'hx-confirm="Pause {people[3].display_name}?' in page

    await client.post(f"/potential-drivers/{first.id}/drove?next=/admin")
    page = html.unescape((await client.get("/admin")).text)
    assert f"hx-confirm=\"Dispute {name}'s kitchen duty on" in page


async def test_admin_folds_revoked_strikes(session, client):
    people = await make_users(session, n=3)
    kept = await strikes.issue(
        session, user_id=people[1].id, reason="still on", today=settings.today()
    )
    gone = await strikes.issue(
        session, user_id=people[2].id, reason="taken back", today=settings.today()
    )
    await strikes.revoke(session, gone, settings.now())
    await session.commit()
    await login(client, "p1")
    page = (await client.get("/admin")).text
    folded = page.split("1 revoked or used")[1].split("</details>")[0]
    assert "taken back" in folded and "still on" not in folded
    assert kept.reason in page


async def test_newcomers_are_told_what_this_is_and_whom_to_ask(session, client):
    await make_users(session, n=3)
    login_page = text(await client.get("/login"))
    assert "Log in with the account your floor speaker gave you" in login_page
    await login(client, "p2")
    help_page = html.unescape(text(await client.get("/help")))
    # The pair rule spelled out, and who the floor speaker is.
    assert "Agree who cleans" in help_page and "you both get a strike" in help_page
    assert "Your floor speaker: Person" in help_page
    stats = text(await client.get("/stats"))
    assert "+100 points = one duty more than your fair share" in stats


async def test_stats_say_where_you_stand(session, client):
    people = await make_users(session, n=4)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, _, p1, _ = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))
    await strikes.issue(session, user_id=p1.id, reason="test", today=MONDAY)
    await session.commit()
    await login(client, p1.username)
    page = text(await client.get("/stats"))
    you = re.sub(r"<[^>]+>", "", page.split('id="you"')[1].split("</p>")[0])
    # p1 cleaned once with 3 riders: two thirds of a duty ahead in the kitchen.
    # One unit with the table (signed duties), in plain words.
    assert "Kitchen: +0.7 duties (more than your share)" in you and "Trash: on par" in you
    assert f"Strikes: 1 of {settings.strike_threshold}" in you
    # The counts are hidden on phones; the table says so there.
    assert 'class="num count"' in page and "More columns on a wider screen" in page


async def test_profile_puts_strikes_first_and_password_last(session, client):
    people = await make_users(session, n=3)
    later = settings.today() + timedelta(days=3)
    await users.add_away(session, people[1], later, later, settings.today())
    await session.commit()
    await login(client, "p2")
    page = html.unescape(text(await client.get("/me")))
    order = [
        page.index(h) for h in ("<h2>Strikes", "<h2>Away", "My recent duties", "Change password")
    ]
    assert order == sorted(order)
    assert "Duties you already have stay yours" in page
    assert f"At {settings.strike_threshold} strikes you get a penalty trash week" in page
    assert 'hx-confirm="Delete your absence from' in page


async def test_admin_sees_and_deletes_absences(session, client):
    people = await make_users(session, n=3)
    later = settings.today() + timedelta(days=10)
    await users.add_away(session, people[2], later, later + timedelta(days=2), settings.today())
    await session.commit()
    away_id = (await session.scalar(select(AwayPeriod))).id
    await login(client, "p1")
    span = f"{short_date(later)}–{long_date(later + timedelta(days=2))}"
    assert span in (await client.get("/admin")).text
    r = await client.post(f"/me/away/{away_id}/delete?next=/admin")
    assert r.headers["location"] == "/admin"
    assert await session.scalar(select(func.count(AwayPeriod.id))) == 0


async def test_htmx_partials_leave_messages_for_the_next_page(session, client):
    await make_users(session, n=2)
    await login(client, "p1")
    await client.post("/admin/tick")  # queues "Daily job done"
    # The plan switch takes only a part of the dashboard: the message must not be used up.
    assert "Daily job done" not in (await client.get("/", headers={"HX-Request": "true"})).text
    page = await client.get("/", headers={"HX-Request": "true", "HX-Boosted": "true"})
    assert "Daily job done" in page.text


async def test_stats_counts_active_strikes(session, client):
    people = await make_users(session, n=3)
    await strikes.issue(session, user_id=people[1].id, reason="x", today=MONDAY)
    revoked = await strikes.issue(session, user_id=people[2].id, reason="x", today=MONDAY)
    await strikes.revoke(session, revoked, at(MONDAY))
    await session.commit()
    assert await strikes.active_counts(session) == {people[1].id: 1}


# --- dishwasher -------------------------------------------------------------

NOVEMBER = date(2026, 11, 1)  # a Sunday; MONDAY is the day after


async def empty_in_turn(session, people, day: date) -> None:
    """Each of `people` empties the dishwasher once on `day`, in order, 2 hours apart."""
    when = at(day, 8)
    for p in people:
        await dishwasher.mark_cleared(session, p, when)
        when += timedelta(hours=2)
    await session.commit()


async def test_dishwasher_cooldown_is_for_everyone(session):
    people = await make_users(session, n=3)
    a, b = people[1], people[2]
    start = at(MONDAY, 10)
    await dishwasher.mark_cleared(session, a, start)
    almost = start + timedelta(minutes=settings.dishwasher_cooldown_minutes - 1)
    with pytest.raises(DishwasherError, match="emptied by Person .* from 11:30"):
        await dishwasher.mark_cleared(session, b, almost)
    with pytest.raises(DishwasherError, match="emptied by you"):
        await dishwasher.mark_cleared(session, a, almost)
    await dishwasher.mark_cleared(session, b, start + timedelta(minutes=90))
    await session.commit()
    # One each; on the tie whoever got there first is ahead.
    assert [(s.user_id, s.count) for s in await dishwasher.standings(session, MONDAY)] == [
        (a.id, 1),
        (b.id, 1),
    ]


async def test_paused_resident_cannot_mark_dishwasher(session):
    people = await make_users(session, n=3)
    await users.set_active(session, people[2], False, at(MONDAY))
    with pytest.raises(DishwasherError, match="Inactive"):
        await dishwasher.mark_cleared(session, people[2], at(MONDAY, 10))


async def test_dishwasher_heroes_are_the_top_of_last_month(session):
    people = await make_users(session, n=6)
    a, b, c, d = people[1:5]
    # a 3, c 2 (reached first), b 2, d 1.
    await empty_in_turn(session, [a, c, c, b, a, b, a, d], date(2026, 10, 20))
    assert await dishwasher.heroes(session, NOVEMBER) == [a.id, c.id]
    assert await dishwasher.heroes(session, date(2026, 11, 30)) == [a.id, c.id]
    # October's heroes come from September, December's from November: none.
    assert await dishwasher.heroes(session, date(2026, 10, 31)) == []
    assert await dishwasher.heroes(session, date(2026, 12, 1)) == []


async def test_dishwasher_months_are_in_local_time(session):
    people = await make_users(session, n=3)
    a, b = people[1], people[2]
    await dishwasher.mark_cleared(session, a, at(date(2026, 10, 31), 22))
    late = datetime.combine(NOVEMBER, time(0, 10), settings.tz)
    await dishwasher.mark_cleared(session, b, late)
    await session.commit()
    assert [s.user_id for s in await dishwasher.standings(session, date(2026, 10, 1))] == [a.id]
    assert [s.user_id for s in await dishwasher.standings(session, NOVEMBER)] == [b.id]


async def test_dishwasher_heroes_get_a_month_off_kitchen_duty(session):
    people = await make_users(session, n=4)
    admin, a, b = people[0], people[1], people[2]
    heroes = {a.id, b.id}
    await empty_in_turn(session, [a, b], date(2026, 10, 20))

    trash_drivers = set()
    day = NOVEMBER
    while day.month == 11:
        await jobs.daily_tick(session, at(day))
        kitchen = await duties.get_trip(session, Duty.kitchen, day)
        await session.refresh(kitchen, ["riders"])
        assert not heroes & {r.user_id for r in kitchen.riders}
        assert not heroes & {pd.user_id for pd in kitchen.potential_drivers}
        trash = await duties.get_trip(session, Duty.trash, day)
        await session.refresh(trash, ["riders"])
        assert heroes <= {r.user_id for r in trash.riders}
        trash_drivers |= {pd.user_id for pd in trash.potential_drivers}
        # The floor speaker marks both, so nobody collects strikes (or a penalty week).
        for trip in (kitchen, trash):
            if not duties.was_driven(trip):
                pd = await duties.get_potential_driver(session, trip.potential_drivers[0].id)
                await duties.mark_as_driver(session, pd, admin, at(day, 20))
        await session.commit()
        day += timedelta(days=1)

    # Trash duty goes on as usual for them.
    assert heroes & trash_drivers
    # The free month costs them nothing.
    balances = await duties.load_balances(session, Duty.kitchen)
    assert all(balances.get(uid, carpool.Balance()).fair_share == 0 for uid in heroes)

    # December: back on kitchen duty.
    await jobs.daily_tick(session, at(date(2026, 12, 1)))
    december = await duties.get_trip(session, Duty.kitchen, date(2026, 12, 1))
    await session.refresh(december, ["riders"])
    assert heroes <= {r.user_id for r in december.riders}


async def test_preview_leaves_out_dishwasher_heroes(session):
    people = await make_users(session, n=6)
    heroes = {people[1].id, people[2].id}
    await empty_in_turn(session, [people[1], people[2]], date(2026, 10, 20))
    await jobs.daily_tick(session, at(MONDAY))

    def planned(preview) -> set[int]:
        return {uid for plan in preview.plans.values() for e in plan for uid in e.potential_drivers}

    window = (MONDAY + timedelta(days=1), MONDAY + timedelta(days=14))
    now_known = await duties.preview(session, Duty.kitchen, *window, MONDAY)
    assert planned(now_known) and not heroes & planned(now_known)
    # Before November, its heroes aren't known yet: November is planned with everyone.
    in_october = await duties.preview(session, Duty.kitchen, *window, date(2026, 10, 30))
    assert heroes & planned(in_october)


async def test_dishwasher_undo_is_for_whoever_marked_it_for_a_short_while(session):
    people = await make_users(session, n=3)
    marked = at(MONDAY, 12)
    clear = await dishwasher.mark_cleared(session, people[1], marked)
    soon = marked + timedelta(minutes=dishwasher.UNDO_MINUTES - 1)
    late = marked + timedelta(minutes=dishwasher.UNDO_MINUTES + 1)
    assert dishwasher.can_undo_clear(clear, people[1], soon)
    assert not dishwasher.can_undo_clear(clear, people[2], soon)
    assert not dishwasher.can_undo_clear(clear, people[1], late)
    with pytest.raises(DishwasherError):
        await dishwasher.undo_clear(session, clear, people[2], soon)
    await dishwasher.undo_clear(session, clear, people[1], soon)
    # Gone, so the next emptying can be marked right away (by anyone).
    await dishwasher.mark_cleared(session, people[2], soon)

    # Not into a new month: the old one may already have decided the heroes.
    end = at(date(2026, 11, 30), 23) + timedelta(minutes=58)
    last = await dishwasher.mark_cleared(session, people[1], end)
    assert not dishwasher.can_undo_clear(last, people[1], end + timedelta(minutes=5))


async def test_admin_deletes_only_this_months_dishwasher_entries(session):
    people = await make_users(session, n=3)
    admin = people[0]
    old = await dishwasher.mark_cleared(session, people[1], at(date(2026, 10, 20)))
    new = await dishwasher.mark_cleared(session, people[2], at(MONDAY))
    await session.commit()
    with pytest.raises(DishwasherError, match="Only the floor speaker"):
        await dishwasher.delete_clear(session, new, people[1], at(MONDAY, 12))
    with pytest.raises(DishwasherError, match="this month"):
        await dishwasher.delete_clear(session, old, admin, at(MONDAY, 12))
    await dishwasher.delete_clear(session, new, admin, at(MONDAY, 12))
    await session.commit()
    assert [c.id for c in await session.scalars(select(DishwasherClear))] == [old.id]


def text(r: httpx.Response) -> str:
    """The page with all whitespace runs as single spaces, to find sentences across lines."""
    return " ".join(r.text.split())


async def test_dishwasher_pages(session, client, monkeypatch):
    people = await make_users(session, n=4)
    # p3 is a hero from last month, shown this month.
    last_month = dishwasher.previous_month(settings.today()).replace(day=15)
    await dishwasher.mark_cleared(session, people[2], at(last_month, 12))
    await session.commit()
    card = {"HX-Request": "true"}

    await login(client, "p2")
    page = text(await client.get("/"))
    assert 'hx-post="/dishwasher?card=1"' in page
    assert "has no kitchen duty in" in page and "dishwasher hero</span>" in page
    # Says what it's for to someone who hasn't emptied it yet.
    assert "Optional: the 2 who empty it most get next month off kitchen duty" in page
    # The card swaps itself, like "Done".
    r = await client.post("/dishwasher?card=1", headers=card)
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = text(await client.get("/"))
    # While it can't be marked: a plain label saying when it can, no (disabled) button.
    assert "Again from" in page and "disabled" not in page
    assert 'hx-post="/dishwasher?card=1"' not in page and 'badge s-drove">done' not in page
    # A mis-tap can be taken back for a few minutes, by whoever marked it.
    clear_id = await session.scalar(
        select(DishwasherClear.id).where(DishwasherClear.user_id == people[1].id)
    )
    undo = f'hx-post="/dishwasher/{clear_id}/undo?card=1"'
    assert undo in page
    assert "You emptied it once this month" in page and "Optional:" not in page
    assert "Last emptied by you today" in page
    # Too soon: the page reloads to show why.
    r = await client.post("/dishwasher?card=1", headers=card)
    assert r.headers.get("hx-refresh") == "true"
    assert "already emptied by you" in text(await client.get("/"))
    stats = text(await client.get("/stats"))
    hero_in = f"on track for {dishwasher.next_month(settings.today()):%B}"
    assert 'id="dishwasher"' in stats and stats.count(hero_in) == 1
    assert "You emptied it once this month" in text(await client.get("/me"))
    assert (await client.post("/admin/dishwasher/1/delete")).status_code == 403

    # The hero is addressed directly; someone else emptied it just now.
    await client.post("/logout")
    await login(client, "p3")
    page = text(await client.get("/"))
    assert "You have no kitchen duty in" in page and "Again from" in page
    assert 'badge s-rode">not needed' not in page and undo not in page
    r = await client.post(f"/dishwasher/{clear_id}/undo?card=1", headers=card)
    assert r.headers.get("hx-refresh") == "true"
    assert "You have no kitchen duty in" in text(await client.get("/me"))

    await users.set_active(session, people[3], False, settings.now())
    await session.commit()
    await client.post("/logout")
    await login(client, "p4")
    assert 'class="badge">inactive' in text(await client.get("/"))

    await client.post("/logout")
    await login(client, "p1")
    clear_id = await session.scalar(
        select(DishwasherClear.id).where(DishwasherClear.user_id == people[1].id)
    )
    assert f"/admin/dishwasher/{clear_id}/delete" in (await client.get("/admin")).text
    r = await client.post(f"/admin/dishwasher/{clear_id}/delete")
    assert r.status_code == 303
    session.expire_all()
    assert await session.scalar(select(func.count(DishwasherClear.id))) == 1

    # Without htmx the whole page comes back with a message.
    monkeypatch.setattr(settings, "dishwasher_cooldown_minutes", 0)
    r = await client.post("/dishwasher")
    assert r.status_code == 303
    assert "Marked as emptied" in text(await client.get("/"))
