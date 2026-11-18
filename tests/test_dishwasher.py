"""The dishwasher list and its monthly heroes."""

from datetime import date, timedelta

import pytest

from app.config import settings
from app.models import Duty
from app.services import carpool, dishwasher, duties, jobs, strikes, telegram, users
from app.services.dishwasher import DishwasherError
from tests.helpers import (
    MONDAY,
    NOVEMBER,
    OCTOBER,
    TUESDAY,
    at,
    make_users,
    planned,
    plans,
    reload_trip,
)


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
    # No cooldown: the next one can be marked right away, by anyone.
    marked = start + timedelta(minutes=1)
    second = await dishwasher.mark_cleared(session, b, marked)
    await session.commit()
    # One each; on the tie whoever got there first is ahead.
    assert await standings(session, MONDAY) == [(a.id, 1), (b.id, 1)]

    # A mis-tap can be taken back by whoever marked it, for a few minutes.
    soon = marked + timedelta(minutes=settings.undo_minutes - 1)
    late = marked + timedelta(minutes=settings.undo_minutes + 1)
    assert not dishwasher.can_undo_clear(second, a, soon)
    assert not dishwasher.can_undo_clear(second, b, late)
    with pytest.raises(DishwasherError):
        await dishwasher.undo_clear(session, second, a, soon)
    await dishwasher.undo_clear(session, second, b, soon)
    # The same person twice in a row counts twice.
    await dishwasher.mark_cleared(session, a, soon)
    await session.commit()
    assert await standings(session, MONDAY) == [(a.id, 2)]

    # The floor speaker can delete entries, only this month's.
    with pytest.raises(DishwasherError, match="Only the floor speaker"):
        await dishwasher.delete_clear(session, first, a, soon)
    with pytest.raises(DishwasherError, match="this month"):
        await dishwasher.delete_clear(session, old, admin, soon)
    await dishwasher.delete_clear(session, first, admin, soon)
    await session.commit()
    assert await standings(session, MONDAY) == [(a.id, 1)]

    # Bursts are for the floor speaker to check: 3 entries less than 90 minutes apart, first
    # to last, whoever made them. Exactly 90 minutes isn't one.
    evening = at(TUESDAY, 18)
    window = timedelta(minutes=settings.dishwasher_suspicious_minutes)
    in_turn = [
        await dishwasher.mark_cleared(session, p, evening + window * k / 2)
        for k, p in enumerate([a, b, a])
    ]
    await session.commit()
    assert dishwasher.suspicious(await dishwasher.clears_in_month(session, TUESDAY)) == set()
    # 5 minutes after the last: a burst with the two before it, but not with the first.
    extra = await dishwasher.mark_cleared(session, b, evening + window + timedelta(minutes=5))
    await session.commit()
    burst = {in_turn[1].id, in_turn[2].id, extra.id}
    clears = await dishwasher.clears_in_month(session, TUESDAY)
    assert dishwasher.suspicious(clears) == burst
    assert {c.id for c in dishwasher.needs_review(clears)} == burst
    # Valid by default: they count until the floor speaker disputes one.
    assert await standings(session, TUESDAY) == [(a.id, 3), (b.id, 2)]

    # Disputing: the floor speaker only, this month only, not twice. The entry stays in the
    # list but no longer counts, also not toward a burst, and gives no strike.
    decided = at(TUESDAY, 21)
    with pytest.raises(DishwasherError, match="Only the floor speaker"):
        await dishwasher.dispute_clear(session, extra, b, decided)
    with pytest.raises(DishwasherError, match="this month"):
        await dishwasher.dispute_clear(session, old, admin, decided)
    await dishwasher.dispute_clear(session, extra, admin, decided)
    with pytest.raises(DishwasherError, match="already disputed"):
        await dishwasher.dispute_clear(session, extra, admin, decided)
    await session.commit()
    assert await standings(session, TUESDAY) == [(a.id, 3), (b.id, 1)]
    clears = await dishwasher.clears_in_month(session, TUESDAY)
    assert extra.id in {c.id for c in clears}
    assert dishwasher.suspicious(clears) == set() and dishwasher.needs_review(clears) == []
    assert await strikes.active_strikes(session, b.id) == []
    # Nor is it the last emptying, and it can't be taken back any more.
    assert (await dishwasher.last_clear(session)).id == in_turn[2].id
    assert not dishwasher.can_undo_clear(extra, b, extra.cleared_at + timedelta(minutes=1))

    # Valid: it counts again, and as checked it no longer needs a decision; the rest of the
    # burst still does.
    with pytest.raises(DishwasherError, match="Only the floor speaker"):
        await dishwasher.confirm_clear(session, extra, b, decided)
    with pytest.raises(DishwasherError, match="this month"):
        await dishwasher.confirm_clear(session, old, admin, decided)
    await dishwasher.confirm_clear(session, extra, admin, decided)
    with pytest.raises(DishwasherError, match="already marked valid"):
        await dishwasher.confirm_clear(session, extra, admin, decided)
    await session.commit()
    assert await standings(session, TUESDAY) == [(a.id, 3), (b.id, 2)]
    clears = await dishwasher.clears_in_month(session, TUESDAY)
    assert dishwasher.suspicious(clears) == burst
    assert {c.id for c in dishwasher.needs_review(clears)} == burst - {extra.id}
    for c in in_turn[1:]:
        await dishwasher.confirm_clear(session, c, admin, decided)
    checked = in_turn[2]
    assert not dishwasher.can_undo_clear(checked, a, checked.cleared_at + timedelta(minutes=1))
    # One more in the checked burst: only that one needs a decision.
    more = await dishwasher.mark_cleared(session, a, extra.cleared_at + timedelta(minutes=1))
    await session.commit()
    clears = await dishwasher.clears_in_month(session, TUESDAY)
    assert [c.id for c in dishwasher.needs_review(clears)] == [more.id]
    # A valid one can still be disputed after all.
    changed_mind = decided + timedelta(minutes=1)
    await dishwasher.dispute_clear(session, extra, admin, changed_mind)
    assert extra.confirmed_at is None and extra.disputed_at == changed_mind

    # No undo into a new month: the old one may already have decided the heroes.
    end = at(date(2026, 11, 30), 23, 58)
    last = await dishwasher.mark_cleared(session, b, end)
    assert not dishwasher.can_undo_clear(last, b, end + timedelta(minutes=5))

    await users.set_active(session, paused, False, end)
    with pytest.raises(DishwasherError, match="Inactive"):
        await dishwasher.mark_cleared(session, paused, end + timedelta(days=1))

    # The floor speaker who decided moves out: the decisions stay.
    await users.delete(session, admin, end)
    await session.commit()
    await session.refresh(extra)
    assert extra.disputed_at == changed_mind and extra.disputed_by_id is None


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


async def test_dishwasher_heroes_get_a_month_off_kitchen_duty(session, bot, monkeypatch):
    people = await make_users(session, n=4)
    admin, a, b = people[0], people[1], people[2]
    heroes = {a.id, b.id}
    await empty_in_turn(session, [a, b], OCTOBER)

    # The preview leaves them out too, once November's heroes are known.
    await jobs.daily_tick(session, at(NOVEMBER))
    # The bot names them in its post on the 1st (a Sunday, so no trash line). Here it
    # posts to the group itself, not a topic of it, and there's no link to the app.
    monkeypatch.setattr(settings, "telegram_thread_id", None)
    monkeypatch.setattr(settings, "app_url", "")
    await telegram.announce_morning(session, at(NOVEMBER))
    kitchen = await duties.get_trip(session, Duty.kitchen, NOVEMBER)
    by_id = {p.id: p for p in people}
    on_duty = " and ".join(by_id[pd.user_id].display_name for pd in kitchen.potential_drivers)
    assert bot.texts[0].splitlines() == [
        f"Kitchen duty today, Sun 1 Nov: {on_duty}. "
        "One of you cleans and marks it done by 08:00 tomorrow.",
        f"Dishwasher heroes of October: {a.display_name} and {b.display_name}. "
        "No kitchen duty for them in November.",
    ]
    assert "message_thread_id" not in bot.sent[0]
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
    # Nobody emptied it in November: no heroes to announce.
    await telegram.announce_morning(session, at(date(2026, 12, 1)))
    assert "heroes" not in bot.texts[-1]
    december = await reload_trip(session, Duty.kitchen, date(2026, 12, 1))
    assert heroes <= {r.user_id for r in december.riders}
