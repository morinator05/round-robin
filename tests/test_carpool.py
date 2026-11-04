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
    Member,
    Role,
    charge_riders,
    credit_driver,
    display_points,
    plan,
    room_key,
)

START = date(2026, 1, 5)  # a Monday


def days(n: int, start: date = START) -> list[tuple[date, date]]:
    """`n` one-day trips from `start`."""
    return [(start + timedelta(days=i),) * 2 for i in range(n)]


def people(n: int) -> list[Member]:
    """`n` members with ids 1..n, nobody away."""
    return [Member(id=i, room=f"A4.{i}") for i in range(1, n + 1)]


@pytest.mark.parametrize("n_people", [3, 7, 15, 30])
def test_counts_stay_within_one(n_people):
    result = plan({}, people(n_people), days(365), 2)
    counts = Counter(uid for chosen in result for uid in chosen)
    assert all(len(set(p)) == 2 for p in result)
    assert max(counts.values()) - min(counts.values()) <= 1


def test_no_double_duty_on_consecutive_days():
    result = plan({}, people(15), days(60), 2)
    for today, tomorrow in pairwise(result):
        assert not set(today) & set(tomorrow)


def test_away_people_are_skipped_and_not_charged():
    members = people(15)
    away_start, away_end = START + timedelta(days=30), START + timedelta(days=89)
    members[0] = Member(id=1, room="A4.1", away=((away_start, away_end),))
    result = plan({}, members, days(180), 2)

    away_days = {d for d, _ in days(180) if away_start <= d <= away_end}
    for (d, _), chosen in zip(days(180), result, strict=True):
        if d in away_days:
            assert 1 not in chosen

    # Afterwards the away person isn't punished with a backlog: their rate while present
    # matches everybody else's (2 of 15 per day).
    counts = Counter(uid for chosen in result for uid in chosen)
    present_days = 180 - len(away_days)
    assert abs(counts[1] - present_days * 2 / 15) <= 1.5


def run(members, n_days, start=START, miss_on=None, balances=None, pick_driver=None):
    """Day-by-day like the real system: one of the potential drivers drives.

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


def test_drive_counts_stay_even_with_random_driver():
    rng = random.Random(42)
    _, _, drivers = run(people(15), 365, pick_driver=rng.choice)
    counts = Counter(drivers)
    assert len(counts) == 15
    assert max(counts.values()) - min(counts.values()) <= 1


def test_lazy_person_is_chosen_more_until_they_drive():
    lazy = 1

    def pick_driver(chosen):
        others = [p for p in chosen if p != lazy]
        return others[0] if others else chosen[0]

    _, chosen_per_day, drivers = run(people(15), 120, pick_driver=pick_driver)
    times_chosen = Counter(uid for c in chosen_per_day for uid in c)
    # Never driving means their score only goes down, so they are chosen every day.
    assert times_chosen[lazy] == 120
    hard_workers = Counter(d for d in drivers if d != lazy)
    assert max(hard_workers.values()) - min(hard_workers.values()) <= 1


def test_missed_trip_comes_up_again_soon():
    members = people(15)
    miss_day = START + timedelta(days=20)
    _, chosen, _ = run(members, 23, miss_on=miss_day)
    missers = set(chosen[20])
    assert missers <= set(chosen[21]) | set(chosen[22])


def test_new_member_does_not_get_backlog():
    members = people(14)
    balances, _, _ = run(members, 70)
    newbie = Member(id=99, room="A4.15")
    _, chosen, _ = run(members + [newbie], 15, START + timedelta(days=70), balances=balances)
    assert sum(99 in c for c in chosen) <= 3


def test_room_natural_sort():
    assert sorted(["A4.10", "A4.2", "A4.1"], key=room_key) == ["A4.1", "A4.2", "A4.10"]


def test_display_points_thirds():
    third = Fraction(1, 3)
    # Equal remainders: the one chosen later (3 is after 1 in the queue) gets the point.
    assert display_points({1: third, 2: -2 * third, 3: third}, queue=[2, 1, 3]) == {
        1: 33,
        2: -67,
        3: 34,
    }


def test_display_points_equal_scores_follow_the_queue():
    # 15 riders, one drove: 14 people at -1/15 (-6.67 points), the driver at 14/15.
    scores = {uid: Fraction(-1, 15) for uid in range(1, 15)} | {15: Fraction(14, 15)}
    queue = list(range(1, 16))
    points = display_points(scores, queue)
    assert sum(points.values()) == 0
    # Equal remainders everywhere: the extra points go to the end of the queue first.
    assert points[15] == 94
    shown = [points[uid] for uid in queue[:-1]]
    assert shown == sorted(shown) and set(shown) == {-7, -6}


def test_display_points_sum_to_zero_and_stay_close():
    members = people(15)
    balances, _, _ = run(members, 200)
    scores = {uid: b.score for uid, b in balances.items()}
    assert sum(scores.values()) == 0
    points = display_points(scores)
    assert sum(points.values()) == 0
    for uid, score in scores.items():
        assert abs(points[uid] - score * POINTS_PER_DUTY) < 1
    # A lower score never shows more points.
    for a, b in itertools.product(scores, repeat=2):
        if scores[a] < scores[b]:
            assert points[a] <= points[b]


def test_fewer_people_than_needed():
    result = plan({}, people(1), days(3), 2)
    assert result == [[1], [1], [1]]


def test_undriven_trip_potential_drivers_become_roles():
    members = people(15)
    balances, _, _ = run(members, 30)
    today = START + timedelta(days=30)
    [pair] = plan(balances, members, [(today, today)], 2)
    charge_riders(balances, [c.id for c in members])

    result = plan(balances, members, days(30, today + timedelta(days=1)), 2, [(today, pair)])
    shown = [u for chosen in result for u in chosen]
    assert not set(pair) & set(shown)
    potential_drivers = tuple(sorted(pair))
    skipped, drove = Role(today, False, potential_drivers), Role(today, True, potential_drivers)
    # Without the drive you're up again much sooner than with it.
    assert skipped in shown and drove in shown
    assert shown.index(skipped) < shown.index(drove)
    assert shown.count(skipped) == shown.count(drove) == 1


def test_fresh_start_non_driver_is_next():
    members = people(15)
    balances = {}
    charge_riders(balances, [c.id for c in members])
    result = plan(balances, members, days(14, START + timedelta(days=1)), 2, [(START, [1, 2])])
    assert Role(START, False, (1, 2)) in result[0]
    # The driver has the highest score, so they're not up again within two weeks.
    assert all(Role(START, True, (1, 2)) not in chosen for chosen in result)


def test_being_chosen_without_driving_does_not_move_you_back():
    members = people(4)
    balances = {}
    for _ in range(2):
        charge_riders(balances, [c.id for c in members])
    # Day 1: 1 and 2 are potential drivers, 1 drives. Day 2: 3 and 4, 3 drives.
    credit_driver(balances, 1, START)
    credit_driver(balances, 3, START + timedelta(days=1))
    # 2 and 4 have the same score; 2 wasn't pushed back by being a potential driver first.
    [chosen] = plan(balances, members, days(1, START + timedelta(days=2)), 1)
    assert chosen == [2]


def test_undriven_trip_with_one_potential_driver_is_kept():
    result = plan({}, people(3), days(2), 1, [(START, [1])])
    assert all(isinstance(u, int) for chosen in result for u in chosen)
