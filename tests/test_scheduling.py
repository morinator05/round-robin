"""The pure fairness logic, simulated over many days without a database."""

import random
from collections import Counter
from datetime import date, timedelta
from itertools import pairwise

import pytest

from app.services.scheduling import Candidate, Role, accrue, credit, plan, room_key

START = date(2026, 1, 5)  # a Monday


def days(n: int, start: date = START) -> list[tuple[date, date]]:
    """`n` one-day slots from `start`."""
    return [(start + timedelta(days=i),) * 2 for i in range(n)]


def people(n: int) -> list[Candidate]:
    """`n` candidates with ids 1..n, nobody away."""
    return [Candidate(id=i, room=f"A4.{i}") for i in range(1, n + 1)]


@pytest.mark.parametrize("n_people", [3, 7, 15, 30])
def test_counts_stay_within_one(n_people):
    result = plan({}, people(n_people), days(365), 2)
    counts = Counter(uid for picked in result for uid in picked)
    assert all(len(set(p)) == 2 for p in result)
    assert max(counts.values()) - min(counts.values()) <= 1


def test_no_double_duty_on_consecutive_days():
    result = plan({}, people(15), days(60), 2)
    for today, tomorrow in pairwise(result):
        assert not set(today) & set(tomorrow)


def test_away_people_are_skipped_and_not_charged():
    cands = people(15)
    away_start, away_end = START + timedelta(days=30), START + timedelta(days=89)
    cands[0] = Candidate(id=1, room="A4.1", away=((away_start, away_end),))
    result = plan({}, cands, days(180), 2)

    away_days = {d for d, _ in days(180) if away_start <= d <= away_end}
    for (d, _), picked in zip(days(180), result, strict=True):
        if d in away_days:
            assert 1 not in picked

    # Afterwards the away person isn't punished with a backlog: their rate while present
    # matches everybody else's (2 of 15 per day).
    counts = Counter(uid for picked in result for uid in picked)
    present_days = 180 - len(away_days)
    assert abs(counts[1] - present_days * 2 / 15) <= 1.5


def run(cands, n_days, start=START, miss_on=None, balances=None, claimer=None):
    """Day-by-day like the real system: one of the picked people claims the job.

    On `miss_on` nobody does it. Returns the balances, the picks and who did each day.
    """
    balances = {} if balances is None else balances
    claimer = claimer or (lambda picked: picked[0])
    picks, doers = [], []
    for d, _ in days(n_days, start):
        [picked] = plan(balances, cands, [(d, d)], 2)
        doer = None if d == miss_on or not picked else claimer(picked)
        # Only a slot someone did counts, as in `duties.load_balances`.
        if doer is not None:
            accrue(balances, [c.id for c in cands if c.is_available(d, d)])
            credit(balances, doer, d)
        picks.append(picked)
        doers.append(doer)
    return balances, picks, doers


def test_done_counts_stay_even_with_random_claimer():
    rng = random.Random(42)
    _, _, doers = run(people(15), 365, claimer=rng.choice)
    counts = Counter(doers)
    assert len(counts) == 15
    assert max(counts.values()) - min(counts.values()) <= 1


def test_lazy_person_is_picked_more_until_they_clean():
    lazy = 1

    def claimer(picked):
        others = [p for p in picked if p != lazy]
        return others[0] if others else picked[0]

    _, picks, doers = run(people(15), 120, claimer=claimer)
    times_picked = Counter(uid for p in picks for uid in p)
    # Never cleaning means never getting credit, so they stay on the list every day.
    assert times_picked[lazy] == 120
    hard_workers = Counter(d for d in doers if d != lazy)
    assert max(hard_workers.values()) - min(hard_workers.values()) <= 1


def test_missed_duty_comes_up_again_soon():
    cands = people(15)
    miss_day = START + timedelta(days=20)
    _, picks, _ = run(cands, 23, miss_on=miss_day)
    missers = set(picks[20])
    assert missers <= set(picks[21]) | set(picks[22])


def test_new_member_does_not_get_backlog():
    cands = people(14)
    balances, _, _ = run(cands, 70)
    newbie = Candidate(id=99, room="A4.15")
    _, picks, _ = run(cands + [newbie], 15, START + timedelta(days=70), balances=balances)
    assert sum(99 in p for p in picks) <= 3


def test_room_natural_sort():
    assert sorted(["A4.10", "A4.2", "A4.1"], key=room_key) == ["A4.1", "A4.2", "A4.10"]


def test_fewer_people_than_needed():
    result = plan({}, people(1), days(3), 2)
    assert result == [[1], [1], [1]]


def test_open_slot_people_become_roles():
    cands = people(15)
    balances, _, _ = run(cands, 30)
    today = START + timedelta(days=30)
    [pair] = plan(balances, cands, [(today, today)], 2)
    accrue(balances, [c.id for c in cands])

    result = plan(balances, cands, days(30, today + timedelta(days=1)), 2, [(today, pair)])
    shown = [u for picked in result for u in picked]
    assert not set(pair) & set(shown)
    members = tuple(sorted(pair))
    skipped, did = Role(today, False, members), Role(today, True, members)
    # Without the credit you're up again much sooner than with it.
    assert skipped in shown and did in shown
    assert shown.index(skipped) < shown.index(did)
    assert shown.count(skipped) == shown.count(did) == 1


def test_fresh_start_non_cleaner_is_next():
    cands = people(15)
    balances = {}
    accrue(balances, [c.id for c in cands])
    result = plan(balances, cands, days(14, START + timedelta(days=1)), 2, [(START, [1, 2])])
    assert Role(START, False, (1, 2)) in result[0]
    # The one who cleans owes the least, so they're not up again within two weeks.
    assert all(Role(START, True, (1, 2)) not in picked for picked in result)


def test_being_picked_without_cleaning_does_not_move_you_back():
    cands = people(4)
    balances = {}
    for _ in range(2):
        accrue(balances, [c.id for c in cands])
    # Day 1: 1 and 2 on duty, 1 cleans. Day 2: 3 and 4, 3 cleans.
    credit(balances, 1, START)
    credit(balances, 3, START + timedelta(days=1))
    # 2 and 4 owe the same; 2 wasn't pushed back by being on duty first.
    [picked] = plan(balances, cands, days(1, START + timedelta(days=2)), 1)
    assert picked == [2]


def test_open_slot_with_one_person_is_kept():
    result = plan({}, people(3), days(2), 1, [(START, [1])])
    assert all(isinstance(u, int) for picked in result for u in picked)
