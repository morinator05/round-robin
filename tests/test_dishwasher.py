"""The dishwasher list and its monthly heroes."""

from datetime import date, timedelta

import pytest

from app.config import settings
from app.models import Duty
from app.services import carpool, dishwasher, duties, jobs, users
from app.services.dishwasher import DishwasherError
from tests.helpers import MONDAY, NOVEMBER, OCTOBER, at, make_users, planned, plans, reload_trip


async def empty_in_turn(session, people, day: date) -> None:
    """Each of `people` empties the dishwasher once on `day`, in order, 2 hours apart."""
    when = at(day, 8)
    for p in people:
        await dishwasher.mark_cleared(session, p, when)
        when += timedelta(hours=2)
    await session.commit()


async def standings(session, day):
    return [(s.user_id, s.count) for s in await dishwasher.standings(session, day)]


async def test_dishwasher_marking(session):
    admin, a, b, paused = await make_users(session, n=4)
    old = await dishwasher.mark_cleared(session, a, at(OCTOBER))
    start = at(MONDAY, 10)
    first = await dishwasher.mark_cleared(session, a, start)

    # One cooldown for everyone, so an emptying isn't marked twice.
    almost = start + timedelta(minutes=settings.dishwasher_cooldown_minutes - 1)
    with pytest.raises(DishwasherError, match="emptied by Person .* from 11:30"):
        await dishwasher.mark_cleared(session, b, almost)
    with pytest.raises(DishwasherError, match="emptied by you"):
        await dishwasher.mark_cleared(session, a, almost)
    marked = start + dishwasher.cooldown()
    second = await dishwasher.mark_cleared(session, b, marked)
    await session.commit()
    # One each; on the tie whoever got there first is ahead.
    assert await standings(session, MONDAY) == [(a.id, 1), (b.id, 1)]

    # A mis-tap can be taken back by whoever marked it, for a few minutes. Then the next
    # emptying can be marked right away.
    soon = marked + timedelta(minutes=dishwasher.UNDO_MINUTES - 1)
    late = marked + timedelta(minutes=dishwasher.UNDO_MINUTES + 1)
    assert not dishwasher.can_undo_clear(second, a, soon)
    assert not dishwasher.can_undo_clear(second, b, late)
    with pytest.raises(DishwasherError):
        await dishwasher.undo_clear(session, second, a, soon)
    await dishwasher.undo_clear(session, second, b, soon)
    await dishwasher.mark_cleared(session, a, soon)

    # The floor speaker can delete entries, only this month's.
    with pytest.raises(DishwasherError, match="Only the floor speaker"):
        await dishwasher.delete_clear(session, first, a, soon)
    with pytest.raises(DishwasherError, match="this month"):
        await dishwasher.delete_clear(session, old, admin, soon)
    await dishwasher.delete_clear(session, first, admin, soon)
    await session.commit()
    assert await standings(session, MONDAY) == [(a.id, 1)]

    # No undo into a new month: the old one may already have decided the heroes.
    end = at(date(2026, 11, 30), 23, 58)
    last = await dishwasher.mark_cleared(session, b, end)
    assert not dishwasher.can_undo_clear(last, b, end + timedelta(minutes=5))

    await users.set_active(session, paused, False, end)
    with pytest.raises(DishwasherError, match="Inactive"):
        await dishwasher.mark_cleared(session, paused, end + timedelta(days=1))


async def test_dishwasher_heroes_are_the_top_of_last_month(session):
    people = await make_users(session, n=6)
    a, b, c, d, e = people[1:]
    # a 3, c 2 (reached first), b 2, d 1.
    await empty_in_turn(session, [a, c, c, b, a, b, a, d], OCTOBER)
    assert await dishwasher.heroes(session, NOVEMBER) == [a.id, c.id]
    assert await dishwasher.heroes(session, date(2026, 11, 30)) == [a.id, c.id]
    # October's heroes come from September, December's from November: none.
    assert await dishwasher.heroes(session, date(2026, 10, 31)) == []
    assert await dishwasher.heroes(session, date(2026, 12, 1)) == []

    # Months are in local time: 22:00 on 31 October is October, 00:10 on 1 November (still
    # October in UTC) is November.
    await dishwasher.mark_cleared(session, e, at(date(2026, 10, 31), 22))
    await dishwasher.mark_cleared(session, b, at(NOVEMBER, 0, 10))
    await session.commit()
    assert (e.id, 1) in await standings(session, OCTOBER)
    assert await standings(session, NOVEMBER) == [(b.id, 1)]


async def test_dishwasher_heroes_get_a_month_off_kitchen_duty(session):
    people = await make_users(session, n=4)
    admin, a, b = people[0], people[1], people[2]
    heroes = {a.id, b.id}
    await empty_in_turn(session, [a, b], OCTOBER)

    # The preview leaves them out too, once November's heroes are known.
    await jobs.daily_tick(session, at(NOVEMBER))
    november = planned(await plans(session, NOVEMBER))
    assert november and not heroes & november
    in_october = await duties.preview(
        session, Duty.kitchen, NOVEMBER, NOVEMBER + timedelta(days=13), date(2026, 10, 30)
    )
    assert heroes & planned(in_october.plans)

    trash_drivers = set()
    day = NOVEMBER
    while day.month == 11:
        await jobs.daily_tick(session, at(day))
        kitchen = await reload_trip(session, Duty.kitchen, day)
        assert not heroes & {r.user_id for r in kitchen.riders}
        assert not heroes & {pd.user_id for pd in kitchen.potential_drivers}
        trash = await reload_trip(session, Duty.trash, day)
        assert heroes <= {r.user_id for r in trash.riders}
        trash_drivers |= {pd.user_id for pd in trash.potential_drivers}
        # The floor speaker marks both, so nobody collects strikes (or a penalty week).
        for trip in (kitchen, trash):
            if not duties.was_driven(trip):
                pd = await duties.get_potential_driver(session, trip.potential_drivers[0].id)
                await duties.mark_as_driver(session, pd, admin, at(day, 20))
        await session.commit()
        day += timedelta(days=1)

    # Trash duty goes on as usual for them, and the free month costs them nothing.
    assert heroes & trash_drivers
    balances = await duties.load_balances(session, Duty.kitchen)
    assert all(balances.get(uid, carpool.Balance()).fair_share == 0 for uid in heroes)

    # December: back on kitchen duty.
    await jobs.daily_tick(session, at(date(2026, 12, 1)))
    december = await reload_trip(session, Duty.kitchen, date(2026, 12, 1))
    assert heroes <= {r.user_id for r in december.riders}
