"""Strikes and penalty weeks."""

from datetime import date, timedelta

import pytest

from app.config import settings
from app.models import AwayPeriod, Duty, Status
from app.services import duties, jobs, strikes, users
from tests.helpers import (
    MONDAY,
    NEXT_MONDAY,
    TUESDAY,
    at,
    give_strikes,
    kitchen_pair,
    make_users,
    penalty_trips,
)


async def test_penalty_week(session):
    people = await make_users(session)
    a, b = people[1], people[2]
    await jobs.daily_tick(session, at(MONDAY))
    await give_strikes(session, a.id)
    await give_strikes(session, b.id)
    await session.commit()

    # The next free weeks, one each, and the strikes are used up by them.
    weeks = [(t.start_date, t.potential_drivers[0].user_id) for t in await penalty_trips(session)]
    assert weeks == [(NEXT_MONDAY, a.id), (NEXT_MONDAY + timedelta(days=7), b.id)]
    assert await strikes.active_strikes(session, a.id) == []

    # A penalty week replaces the regular trash duty, and serving it isn't a regular drive.
    await jobs.daily_tick(session, at(NEXT_MONDAY))
    trip = await duties.get_trip(session, Duty.trash, NEXT_MONDAY)
    assert trip.is_penalty and [pd.user_id for pd in trip.potential_drivers] == [a.id]
    pd = await duties.get_potential_driver(session, trip.potential_drivers[0].id)
    await duties.mark_as_driver(session, pd, a, at(NEXT_MONDAY, 20))
    await session.commit()
    balances = await duties.load_balances(session, Duty.trash)
    assert all(b.drives == 0 and b.score == 0 for b in balances.values())


async def test_penalty_week_follows_its_person(session):
    people = await make_users(session, n=4)
    target, other_id = people[1], people[2].id
    await give_strikes(session, target.id)
    [trip] = await penalty_trips(session)
    week = trip.start_date

    # Away in that week: it moves to the next free week.
    await users.add_away(session, target, week, week + timedelta(days=2), MONDAY)
    [trip] = await penalty_trips(session)
    assert trip.start_date == week + timedelta(days=7)
    await session.refresh(target)  # `add_away` expired everything

    # Paused: no penalty week until they are back, and their strikes count again.
    await users.set_active(session, target, False, at(MONDAY, 12))
    assert await penalty_trips(session) == []
    assert len(await strikes.active_strikes(session, target.id)) == settings.strike_threshold
    await users.set_active(session, target, True, at(MONDAY, 12))
    [trip] = await penalty_trips(session)

    # Moving out: that week gets a regular trash duty again.
    await users.delete(session, target, at(MONDAY, 12))
    await session.commit()
    assert await penalty_trips(session) == []
    await jobs.daily_tick(session, at(trip.start_date))
    regular = await duties.get_trip(session, Duty.trash, trip.start_date)
    assert not regular.is_penalty and len(regular.potential_drivers) == 1

    # An endless absence (e.g. from before the limit) doesn't make the search run forever.
    session.add(AwayPeriod(user_id=other_id, start_date=MONDAY, end_date=date(9999, 12, 31)))
    await session.flush()
    await give_strikes(session, other_id)
    [trip] = await penalty_trips(session)
    assert trip.start_date == NEXT_MONDAY


@pytest.mark.parametrize("decision", ["mark_done", "excuse", "revoke"])
async def test_late_decision_takes_strikes_back(session, decision):
    """A missed duty gives the last strike and a penalty week; deciding later that it
    shouldn't count takes both back."""
    people = await make_users(session)
    admin = people[0]
    pair = await kitchen_pair(session, people, admin_off=True)
    await give_strikes(session, pair.p1.id, settings.strike_threshold - 1)
    await session.commit()
    await jobs.daily_tick(session, at(TUESDAY))
    assert len(await penalty_trips(session)) == 1
    assert await strikes.active_strikes(session, pair.p1.id) == []

    first = await duties.get_potential_driver(session, pair.first.id)
    second = next(pd for pd in first.trip.potential_drivers if pd is not first)
    assert first.status is second.status is Status.missed
    later = at(TUESDAY, 12)
    if decision == "mark_done":
        await duties.mark_as_driver(session, first, admin, later)
    elif decision == "excuse":
        await duties.excuse(session, first, admin, later)
    else:
        [strike] = await strikes.for_potential_driver(session, first.id)
        await strikes.revoke(session, strike, later)
    await session.commit()

    assert await penalty_trips(session) == []
    counts = await strikes.active_counts(session)
    assert counts[pair.p1.id] == settings.strike_threshold - 1
    if decision == "mark_done":
        # The partner didn't have to clean after all: their strike goes too.
        assert second.status is Status.rode and pair.p2.id not in counts
    else:
        assert second.status is Status.missed and counts[pair.p2.id] == 1
