"""Duties against a real database: choosing, deadlines, marking done, undo, preview."""

import random
from datetime import date, timedelta
from fractions import Fraction

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import AwayPeriod, Duty, Status, Trip, User
from app.services import carpool, duties, jobs, strikes, users
from app.services.duties import DutyError
from tests.helpers import (
    MONDAY,
    NEXT_MONDAY,
    TUESDAY,
    WEDNESDAY,
    at,
    kitchen_pair,
    make_users,
    planned,
    plans,
    reload_trip,
)


async def test_daily_tick_and_deadlines(session):
    await make_users(session)
    hour = settings.deadline_hour
    await jobs.daily_tick(session, at(MONDAY, hour))
    monday = await duties.get_trip(session, Duty.kitchen, MONDAY)
    trash = await duties.get_trip(session, Duty.trash, MONDAY + timedelta(days=3))
    assert len(monday.potential_drivers) == 2 and len(trash.potential_drivers) == 1
    assert (trash.start_date, trash.end_date) == (MONDAY, MONDAY + timedelta(days=6))
    chosen = [pd.user_id for pd in monday.potential_drivers]
    await jobs.daily_tick(session, at(MONDAY, 13))
    assert len((await session.scalars(select(Trip))).all()) == 2  # idempotent

    # Tuesday before the deadline hour: Monday's duty is still the current one, and not
    # missed yet. Nothing chooses Tuesday's yet, not even a floor speaker's action.
    early = at(TUESDAY, hour - 1)
    await jobs.daily_tick(session, early)
    await duties.fill_current(session, early)
    assert await duties.get_trip(session, Duty.kitchen, TUESDAY) is None
    assert (await duties.current_trip(session, Duty.kitchen, early)).id == monday.id
    monday = await reload_trip(session, Duty.kitchen, MONDAY)
    assert all(pd.status is Status.pending for pd in monday.potential_drivers)

    # At the deadline hour Monday's is missed, a strike each, before Tuesday's is chosen.
    await jobs.daily_tick(session, at(TUESDAY, hour))
    monday = await reload_trip(session, Duty.kitchen, MONDAY)
    assert all(pd.status is Status.missed for pd in monday.potential_drivers)
    for uid in chosen:
        assert len(await strikes.active_strikes(session, uid)) == 1
    assert len((await duties.get_trip(session, Duty.kitchen, TUESDAY)).potential_drivers) == 2

    # Trash duty is due the next Monday at the deadline hour.
    sunday_night = at(NEXT_MONDAY, hour - 1)
    await jobs.daily_tick(session, sunday_night)
    assert (await duties.current_trip(session, Duty.trash, sunday_night)).id == trash.id
    trash = await reload_trip(session, Duty.trash, MONDAY)
    assert trash.potential_drivers[0].status is Status.pending
    await jobs.daily_tick(session, at(NEXT_MONDAY, hour))
    trash = await reload_trip(session, Duty.trash, MONDAY)
    assert trash.potential_drivers[0].status is Status.missed


async def test_top_up(session, monkeypatch):
    """A trip that is short of people gets more as residents move in, but only while it is
    on: not once someone drove it, and not after its day."""
    monkeypatch.setattr(settings, "kitchen_people_per_day", 3)
    hour = settings.deadline_hour
    # The daily job ran before anyone lived on the floor.
    await jobs.daily_tick(session, at(MONDAY))
    assert (await duties.get_trip(session, Duty.kitchen, MONDAY)).potential_drivers == []
    [first] = await make_users(session, n=1)
    await jobs.daily_tick(session, at(MONDAY, 10))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    assert [pd.user_id for pd in kitchen.potential_drivers] == [first.id]
    assert len((await duties.get_trip(session, Duty.trash, MONDAY)).potential_drivers) == 1

    async def move_in(username, now):
        room = f"E1{20 + len(username)}"
        new = await users.create(
            session, name="New", room_number=room, username=username, password="secret123"
        )
        await duties.fill_current(session, now)  # like adding a resident on the admin page
        await session.commit()
        return new

    # Done already: nobody is added to it (and struck for it).
    pd = await duties.get_potential_driver(session, kitchen.potential_drivers[0].id)
    await duties.mark_as_driver(session, pd, first, at(MONDAY, 11))
    await session.commit()
    await move_in("second", at(MONDAY, 12))
    kitchen = await reload_trip(session, Duty.kitchen, MONDAY)
    assert [pd.user_id for pd in kitchen.potential_drivers] == [first.id]

    # Tuesday's is one short. Moving in the next morning doesn't put you on it: its day is
    # over, there's only an hour left to do it.
    await jobs.daily_tick(session, at(TUESDAY, hour))
    assert len((await reload_trip(session, Duty.kitchen, TUESDAY)).potential_drivers) == 2
    third = await move_in("third", at(WEDNESDAY, hour - 1))
    tuesday = await reload_trip(session, Duty.kitchen, TUESDAY)
    assert third.id not in {pd.user_id for pd in tuesday.potential_drivers}
    assert third.id not in {r.user_id for r in tuesday.riders}
    await jobs.daily_tick(session, at(WEDNESDAY, hour))
    assert await strikes.active_strikes(session, third.id) == []


async def test_marking_done(session):
    people = await make_users(session)
    admin = people[0]
    pair = await kitchen_pair(session, people, admin_off=True)
    outsider = next(p for p in people[1:] if p.id not in pair.ids)

    # Before anyone cleans, each of the pair holds half the trip for choosing, the scores
    # stay 0, and tomorrow's pair is somebody else.
    balances = await duties.load_balances(session, Duty.kitchen)
    assert (
        balances[pair.p1.id].pending_share == balances[pair.p2.id].pending_share == Fraction(1, 2)
    )
    assert all(b.score == 0 for b in balances.values())
    tuesday = await duties.materialize(session, Duty.kitchen, TUESDAY)
    assert {pd.user_id for pd in tuesday.potential_drivers}.isdisjoint(pair.ids)

    for not_theirs in (outsider, pair.p2):
        with pytest.raises(DutyError):
            await duties.mark_as_driver(session, pair.first, not_theirs, at(MONDAY, 20))
    await duties.mark_as_driver(session, pair.first, pair.p1, at(MONDAY, 20))
    assert pair.first.status is Status.drove and pair.first.completed_by_id == pair.p1.id
    assert pair.second.status is Status.rode
    with pytest.raises(DutyError):  # closed now
        await duties.mark_as_driver(session, pair.second, pair.p2, at(MONDAY, 21))
    await session.commit()

    # One drive for the cleaner, a share (1/14, the floor speaker was away) for every rider.
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[pair.p1.id].score == 1 - Fraction(1, 14)
    assert balances[pair.p2.id].score == -Fraction(1, 14)
    assert sum(b.score for b in balances.values()) == 0
    # Trash duty is counted on its own.
    assert all(b.score == 0 for b in (await duties.load_balances(session, Duty.trash)).values())

    # After the deadline: no strike for the partner. Tuesday's pair is too late now; the
    # floor speaker can still mark it.
    later = at(WEDNESDAY, 12)
    await jobs.daily_tick(session, later)
    assert await strikes.active_strikes(session, pair.p2.id) == []
    late = next(pd for pd in tuesday.potential_drivers if pd.user_id != admin.id)
    late = await duties.get_potential_driver(session, late.id)
    with pytest.raises(DutyError):
        await duties.mark_as_driver(session, late, late.user, later)
    await duties.mark_as_driver(session, late, admin, later)
    assert late.status is Status.drove


async def test_undo_done(session):
    people = await make_users(session)
    admin = people[0]
    pair = await kitchen_pair(session, people, admin_off=True)
    marked = at(MONDAY, 20)
    await duties.mark_as_driver(session, pair.first, pair.p1, marked)

    # Only whoever marked it (and the floor speaker), only for a few minutes.
    soon = marked + timedelta(minutes=settings.undo_minutes - 1)
    late = marked + timedelta(minutes=settings.undo_minutes + 1)
    assert not duties.can_undo(pair.first, pair.p2, soon)
    assert duties.can_undo(pair.first, admin, soon)
    assert not duties.can_undo(pair.first, pair.p1, late)
    with pytest.raises(DutyError):
        await duties.undo_driver(session, pair.first, pair.p1, late)

    # Open again for both of them.
    await duties.undo_driver(session, pair.first, pair.p1, soon)
    assert pair.first.status is pair.second.status is Status.pending
    assert pair.first.completed_at is None and pair.first.completed_by_id is None
    assert duties.can_mark_as_driver(pair.second, pair.p2, soon)

    # An excused duty the floor speaker marked done by mistake is excused again after the
    # undo, as it was: same time, same floor speaker.
    excused = at(MONDAY, 21)
    await duties.excuse(session, pair.first, admin, excused)
    await duties.mark_as_driver(session, pair.first, admin, excused + timedelta(minutes=1))
    await duties.undo_driver(session, pair.first, admin, excused + timedelta(minutes=2))
    assert pair.first.status is Status.excused
    assert (pair.first.completed_at, pair.first.completed_by_id) == (excused, admin.id)
    assert pair.second.status is Status.pending

    # Never after the deadline, not even by the floor speaker.
    deadline = duties.deadline(pair.first.trip)
    await duties.mark_as_driver(session, pair.second, pair.p2, deadline - timedelta(minutes=2))
    assert not duties.can_undo(pair.second, pair.p2, deadline)
    assert not duties.can_undo(pair.second, admin, deadline + timedelta(minutes=1))


async def test_dispute(session):
    people = await make_users(session)
    admin = people[0]
    pair = await kitchen_pair(session, people, admin_off=True)
    await duties.mark_as_driver(session, pair.first, pair.p1, at(MONDAY, 20))

    with pytest.raises(DutyError):
        await duties.dispute(session, pair.first, pair.p2, at(MONDAY, 21))
    await duties.dispute(session, pair.first, admin, at(MONDAY, 21))
    await session.commit()
    assert pair.first.status is Status.missed and pair.first.disputed_at is not None
    assert pair.second.status is Status.rode
    [strike] = await strikes.active_strikes(session, pair.p1.id)
    assert strike.issued_by_id == admin.id and strike.potential_driver_id == pair.first.id

    # The floor speaker gives the partner the credit: the disputed one keeps the strike,
    # and with a strike on the trip none of it can be undone.
    second = await duties.get_potential_driver(session, pair.second.id)
    await duties.mark_as_driver(session, second, admin, at(MONDAY, 22))
    first = await duties.get_potential_driver(session, pair.first.id)
    assert second.status is Status.drove and first.status is Status.missed
    assert not duties.can_undo(second, admin, at(MONDAY, 22, 1))
    await session.commit()
    # The deadline passing later doesn't add a second strike.
    await jobs.daily_tick(session, at(WEDNESDAY))
    assert len(await strikes.active_strikes(session, pair.p1.id)) == 1


@pytest.mark.parametrize("how", ["missed", "excused", "disputed"])
async def test_undriven_trip_counts_for_nobody(session, how):
    people = await make_users(session)
    admin = people[0]
    pair = await kitchen_pair(session, people, admin_off=True)
    if how == "excused":
        for pd in (pair.first, pair.second):
            await duties.excuse(session, pd, admin, at(MONDAY, 12))
    elif how == "disputed":
        await duties.mark_as_driver(session, pair.first, pair.p1, at(MONDAY, 20))
        await duties.dispute(session, pair.first, admin, at(MONDAY, 21))
    await session.commit()
    await jobs.daily_tick(session, at(TUESDAY, 13))  # past the deadline
    balances = await duties.load_balances(session, Duty.kitchen)
    assert all(b.score == 0 for b in balances.values())


@pytest.mark.parametrize(
    ("action", "by_admin", "status", "error"),
    [
        ("excuse", True, Status.drove, "Only open or missed duties"),
        ("excuse", False, Status.pending, "Only the floor speaker"),
        ("keep_missed", True, Status.pending, "Only missed duties"),
        ("keep_missed", False, Status.missed, "Only the floor speaker"),
        ("dispute", True, Status.pending, "Only duties marked as done"),
    ],
)
async def test_admin_action_guards(session, action, by_admin, status, error):
    people = await make_users(session, n=4)
    pd = (await kitchen_pair(session, people)).first
    pd.status = status
    actor = people[0] if by_admin else people[1]
    with pytest.raises(DutyError, match=error):
        await getattr(duties, action)(session, pd, actor, at(MONDAY, 12))
    assert pd.status is status


async def test_preview_plans(session):
    people = await make_users(session)
    admin = people[0]
    pair = await kitchen_pair(session, people)

    # One plan per pending potential driver: whoever doesn't clean is up again tomorrow,
    # whoever does much later.
    before = await plans(session, MONDAY, days=30)
    assert set(before) == pair.ids
    for driver, other in [(pair.p1, pair.p2), (pair.p2, pair.p1)]:
        assert other.id in before[driver.id][0].potential_drivers
        assert all(driver.id not in e.potential_drivers for e in before[driver.id][:7])

    # Once it's done the outcome is known: a single plan, the one for whoever cleaned.
    await duties.mark_as_driver(session, pair.first, pair.p1, at(MONDAY, 20))
    after = await plans(session, MONDAY, days=30)
    assert list(after) == [None] and after[None] == before[pair.p1.id]

    # Undone: open again, a plan per person again. One excused: only the other's is left.
    await duties.undo_driver(session, pair.first, pair.p1, at(MONDAY, 20, 5))
    assert set(await plans(session, MONDAY)) == pair.ids
    await duties.excuse(session, pair.first, admin, at(MONDAY, 21))
    assert list(await plans(session, MONDAY)) == [pair.p2.id]


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
            await users.add_away(session, people[5], day, day + timedelta(days=3), at(day))
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


SEED = 3  # gives a pair with different scores (the fair choice usually picks equal ones)


@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("next_morning", [False, True], ids=["evening", "next-morning"])
async def test_each_plan_is_what_really_happens(session, which, next_morning):
    """Each plan is exactly what gets chosen tomorrow once that one marks today's duty."""
    people = await make_users(session)
    by_id = {p.id: p for p in people}
    today = await history(session, people, 12, SEED)
    tomorrow = today + timedelta(days=1)
    hour = settings.deadline_hour
    await jobs.daily_tick(session, at(today, hour))
    trip = await duties.get_trip(session, Duty.kitchen, today)
    pair = [pd for pd in trip.potential_drivers if pd.status is Status.pending]
    balances = await duties.load_balances(session, Duty.kitchen)
    scores = {balances.get(pd.user_id, carpool.Balance()).score for pd in pair}
    assert len(pair) == 2 and len(scores) == 2

    preview = await duties.preview(
        session, Duty.kitchen, tomorrow, today + timedelta(days=14), today
    )
    assert preview.open_start == today
    assert list(preview.plans) == [pd.user_id for pd in pair]

    driver = await duties.get_potential_driver(session, pair[which].id)
    # Marked in the evening, or the next morning before the deadline hour.
    marked = at(tomorrow, hour - 1) if next_morning else at(today, 20)
    await duties.mark_as_driver(session, driver, by_id[driver.user_id], marked)
    await session.commit()
    if not next_morning:
        assert (await plans(session, today))[None] == preview.plans[driver.user_id]

    await jobs.daily_tick(session, at(tomorrow, hour))
    chosen = await duties.get_trip(session, Duty.kitchen, tomorrow)
    real = [pd.user_id for pd in chosen.potential_drivers]
    assert real == preview.plans[driver.user_id][0].potential_drivers


@pytest.mark.parametrize("next_morning", [False, True], ids=["same-day", "next-morning"])
@pytest.mark.parametrize("how", ["away", "paused", "deleted"])
async def test_unavailable_resident(session, how, next_morning):
    """Someone on Monday's kitchen duty and someone who only rides on it become unavailable,
    on Monday or on Tuesday morning, before its deadline."""
    people = await make_users(session, n=5)
    pair = await kitchen_pair(session, people)
    on_duty_id, partner_id = pair.p1.id, pair.p2.id
    rider_id = next(p.id for p in people if p.id not in pair.ids)
    now = at(TUESDAY, settings.deadline_hour - 1) if next_morning else at(MONDAY, 12)
    # The one on duty first: the change for the other one must not undo what it decided.
    for person in (pair.p1, await session.get(User, rider_id)):
        if how == "away":
            await users.add_away(session, person, MONDAY, MONDAY + timedelta(days=5), now)
        elif how == "paused":
            await users.set_active(session, person, False, now)
        else:
            await users.delete(session, person, now)
    await session.commit()

    trip = await reload_trip(session, Duty.kitchen, MONDAY)
    riders = [r.user_id for r in trip.riders]
    on_duty = [pd.user_id for pd in trip.potential_drivers]
    # Riding no more, so they owe no share of it.
    assert rider_id not in riders
    # Not chosen while unavailable.
    assert on_duty_id not in planned(await plans(session, now.date(), days=3))
    if how == "away":
        # A duty you already have stays yours (the floor speaker can excuse it).
        assert on_duty_id in on_duty and on_duty_id in riders
    else:
        assert on_duty_id not in on_duty and on_duty_id not in riders
        if next_morning:
            # Too late for a stand-in: just off it, so no strike at the deadline.
            assert on_duty == [partner_id]
        else:
            # Replaced right away; the trip keeps its two.
            assert len(on_duty) == 2 and None not in on_duty
    if how == "deleted":
        # Their part of the history stays, without their name.
        assert riders.count(None) == 2
    if how == "paused" and next_morning:
        await jobs.daily_tick(session, at(TUESDAY, settings.deadline_hour))
        assert await strikes.active_strikes(session, on_duty_id) == []

    # Available again the same day: a rider again, so they owe their share again.
    if how != "deleted" and not next_morning:
        rider = await session.get(User, rider_id)
        if how == "away":
            away = await session.scalar(select(AwayPeriod).where(AwayPeriod.user_id == rider_id))
            await users.remove_away(session, away, now)
        else:
            await users.set_active(session, rider, True, now)
        trip = await reload_trip(session, Duty.kitchen, MONDAY)
        assert rider_id in {r.user_id for r in trip.riders}


async def test_fair_month_then_moving_out(session):
    """30 days, one random person of each pair cleans: everybody cleans twice."""
    rng = random.Random(7)
    people = await make_users(session)
    day = MONDAY
    for _ in range(30):
        await jobs.daily_tick(session, at(day))
        trip = await duties.get_trip(session, Duty.kitchen, day)
        pd = await duties.get_potential_driver(session, rng.choice(trip.potential_drivers).id)
        await duties.mark_as_driver(session, pd, pd.user, at(day, 20))
        await session.commit()
        day += timedelta(days=1)
    before = await duties.load_balances(session, Duty.kitchen)
    drives = [b.drives for b in before.values()]
    assert len(before) == 15 and max(drives) - min(drives) <= 1
    assert sum(b.score for b in before.values()) == 0

    # Moving out (on the last scheduled day, so no new trip changes anything): everyone
    # else's score stays, the leaver's leaves with them.
    leaving_id = people[1].id
    await users.delete(session, people[1], at(day - timedelta(days=1), 12))
    await session.commit()
    after = {uid: b.score for uid, b in (await duties.load_balances(session, Duty.kitchen)).items()}
    assert after == {uid: b.score for uid, b in before.items() if uid != leaving_id}
    assert sum(after.values()) == -before[leaving_id].score
