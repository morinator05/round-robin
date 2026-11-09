"""The pure carpool logic, simulated over many days without a database."""

import itertools
import random
from collections import Counter
from datetime import date, timedelta
from fractions import Fraction
from itertools import pairwise

import pytest

from app.services.carpool import (
    POINTS_PER_DUTY,
    Balance,
    Member,
    assume_driven,
    charge_riders,
    credit_driver,
    display_points,
    plan,
)

START = date(2026, 1, 5)  # a Monday


def days(n: int, start: date = START) -> list[tuple[date, date]]:
    """`n` one-day trips from `start`."""
    return [(start + timedelta(days=i),) * 2 for i in range(n)]


def people(n: int) -> list[Member]:
    """`n` members with ids 1..n in rooms E101.., nobody away."""
    return [Member(id=i, room=f"E1{i:02d}") for i in range(1, n + 1)]


def run(members, n_days, start=START, miss_on=None, balances=None, pick_driver=None):
    """Day by day like the real system: one of the potential drivers drives.

    On `miss_on` nobody drives. Returns the balances, the potential drivers and the driver
    of each day.
    """
    balances = {} if balances is None else balances
    pick_driver = pick_driver or (lambda chosen: chosen[0])
    chosen_per_day, drivers = [], []
    for d, _ in days(n_days, start):
        [chosen] = plan(balances, members, [(d, d)], 2)
        driver = None if d == miss_on or not chosen else pick_driver(chosen)
        # Only a driven trip counts, as in `duties.load_balances`.
        if driver is not None:
            charge_riders(balances, [m.id for m in members if m.rides(d, d)])
            credit_driver(balances, driver, d)
        chosen_per_day.append(chosen)
        drivers.append(driver)
    return balances, chosen_per_day, drivers


def spread(counts: Counter) -> int:
    return max(counts.values()) - min(counts.values())


@pytest.mark.parametrize("n_people", [3, 15, 30])
def test_simulated_year_is_even(n_people):
    members = people(n_people)
    planned = plan({}, members, days(365), 2)
    assert all(len(set(pair)) == 2 for pair in planned)
    assert spread(Counter(uid for pair in planned for uid in pair)) <= 1
    if n_people >= 4:
        for today, tomorrow in pairwise(planned):
            assert not set(today) & set(tomorrow)

    # Whichever of the pair drives, everyone drives equally often.
    balances, _, drivers = run(members, 365, pick_driver=random.Random(42).choice)
    counts = Counter(drivers)
    assert len(counts) == n_people and spread(counts) <= 1

    # The scores sum to 0, and so do the points shown for them, which stay close to the
    # scores and keep their order.
    scores = {uid: b.score for uid, b in balances.items()}
    assert sum(scores.values()) == 0
    points = display_points(scores)
    assert sum(points.values()) == 0
    for uid, score in scores.items():
        assert abs(points[uid] - score * POINTS_PER_DUTY) < 1
    for a, b in itertools.product(scores, repeat=2):
        if scores[a] < scores[b]:
            assert points[a] <= points[b]


def test_absent_or_new_member_owes_no_backlog():
    # Away for two months: never chosen then, and afterwards chosen at everybody's rate
    # (2 of 15 per day) instead of making up for it.
    members = people(15)
    away_start, away_end = START + timedelta(days=30), START + timedelta(days=89)
    members[0] = Member(id=1, room="E101", away=((away_start, away_end),))
    result = plan({}, members, days(180), 2)
    away_days = {d for d, _ in days(180) if away_start <= d <= away_end}
    for (d, _), chosen in zip(days(180), result, strict=True):
        assert d not in away_days or 1 not in chosen
    times = sum(1 in chosen for chosen in result)
    assert abs(times - (180 - len(away_days)) * 2 / 15) <= 1.5

    # Joining late is like having been away before: no catching up.
    members = people(14)
    balances, _, _ = run(members, 70)
    newbie = Member(id=99, room="E199")
    _, chosen, _ = run(members + [newbie], 15, START + timedelta(days=70), balances=balances)
    assert sum(99 in c for c in chosen) <= 3


def test_non_drivers_come_back_soon():
    lazy = 1

    def everyone_but_lazy(chosen):
        others = [p for p in chosen if p != lazy]
        return others[0] if others else chosen[0]

    _, chosen_per_day, drivers = run(people(15), 120, pick_driver=everyone_but_lazy)
    # Never driving means their score only goes down, so they are chosen every day.
    assert sum(lazy in c for c in chosen_per_day) == 120
    assert spread(Counter(d for d in drivers if d != lazy)) <= 1

    # A day nobody drove: the same pair is up again within two days.
    miss_day = START + timedelta(days=20)
    _, chosen, _ = run(people(15), 23, miss_on=miss_day)
    assert set(chosen[20]) <= set(chosen[21]) | set(chosen[22])


def test_ties():
    tomorrow = days(1, START + timedelta(days=2))
    # Fewer people than needed: whoever is there.
    assert plan({}, people(1), days(3), 2) == [[1], [1], [1]]

    # Day 1: 1 and 2 are potential drivers, 1 drives. Day 2: 3 and 4, 3 drives. 2 and 4 now
    # have the same score; 2 wasn't pushed back by having been chosen first.
    members = people(4)
    balances = {}
    for _ in range(2):
        charge_riders(balances, [m.id for m in members])
    credit_driver(balances, 1, START)
    credit_driver(balances, 3, START + timedelta(days=1))
    assert plan(balances, members, tomorrow, 1) == [[2]]

    # Equal scores: whoever never drove goes before whoever drove, even with a later room.
    balances = {1: Balance(drives=Fraction(1), fair_share=Fraction(1), last_drove=START)}
    assert plan(balances, people(2), tomorrow, 1) == [[2]]
    # Otherwise the room decides.
    assert plan({}, [Member(id=1, room="E102"), Member(id=2, room="E101")], tomorrow, 1) == [[2]]


def test_display_points_rounding():
    third = Fraction(1, 3)
    # Equal remainders: the one chosen later (3 is after 1 in the queue) gets the point.
    assert display_points({1: third, 2: -2 * third, 3: third}, queue=[2, 1, 3]) == {
        1: 33,
        2: -67,
        3: 34,
    }

    # 15 riders, one drove: 14 people at -1/15 (-6.67 points), the driver at 14/15.
    scores = {uid: Fraction(-1, 15) for uid in range(1, 15)} | {15: Fraction(14, 15)}
    queue = list(range(1, 16))
    points = display_points(scores, queue)
    assert sum(points.values()) == 0 and points[15] == 94
    # Equal remainders everywhere: the extra points go to the end of the queue first.
    shown = [points[uid] for uid in queue[:-1]]
    assert shown == sorted(shown) and set(shown) == {-7, -6}


def first_day(result: list[list[int]], uid: int, start: date) -> date | None:
    """The first trip (as a date) in a plan of one-day trips from `start` that `uid` is on."""
    return next((start + timedelta(days=i) for i, c in enumerate(result) if uid in c), None)


def test_open_trip_plans():
    """Each outcome of the open trip is simulated exactly, with the real balances."""
    half = Fraction(1, 2)
    balances = {1: Balance(pending_share=half), 2: Balance(pending_share=half)}
    released = {1: half, 2: half}
    after = assume_driven(balances, [1, 2, 3, 4], released, 1, START)
    assert after[1].score == 1 - Fraction(1, 4) and after[1].last_drove == START
    assert after[2].score == after[3].score == -Fraction(1, 4)
    assert after[1].pending_share == after[2].pending_share == 0
    assert balances[1] == Balance(pending_share=half)  # the input is not changed

    # Fresh start: whoever doesn't drive is up next, the driver not within two weeks.
    members = people(15)
    upcoming = days(14, START + timedelta(days=1))
    for driver, other in [(1, 2), (2, 1)]:
        after = assume_driven(balances, range(1, 16), released, driver, START)
        result = plan(after, members, upcoming, 2)
        assert other in result[0]
        assert all(driver not in chosen for chosen in result)

    # A pair far apart: the lowest and a higher score, both reserving half of today's trip.
    balances, _, _ = run(members, 40)
    today = START + timedelta(days=40)
    by_score = sorted(balances, key=lambda uid: balances[uid].score)
    low, high = by_score[0], by_score[7]
    released = {low: half, high: half}
    for uid, share in released.items():
        balances[uid].pending_share += share
    start = today + timedelta(days=1)
    riders = [m.id for m in members]
    plans = {
        driver: plan(
            assume_driven(balances, riders, released, driver, today), members, days(30, start), 2
        )
        for driver in (low, high)
    }
    assert plans[low] != plans[high]
    for driver, other in [(low, high), (high, low)]:
        assert first_day(plans[driver], other, start) < (
            first_day(plans[driver], driver, start) or date.max
        )
    # Whoever skips is up sooner the lower their score is.
    assert first_day(plans[high], low, start) <= first_day(plans[low], high, start)
