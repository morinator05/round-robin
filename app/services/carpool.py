"""Fair duty assignment: the fair carpool algorithm, with extensions.

Base: R. Fagin and J. H. Williams, "A Fair Carpool Scheduling Algorithm", IBM Journal of
Research and Development 27(2), 1983. A carpool makes one trip per day. The k members
riding that day each owe a fair share of 1/k of the drive, and the driver earns 1. A
member's score is drives minus fair shares, so the scores always sum to 0, and whoever
has the lowest score drives next.

Here each duty is a carpool of its own (kitchen and trash are counted separately):
- A trip is one kitchen day or one trash week.
- The riders are the members available for it (active, not away). Whoever is away or
  paused isn't in the car that day: no share, so being away is automatically fair.
- The driver is whoever marks the trip done.

Our extensions:
- Each trip gets `n` potential drivers (kitchen: 2, trash: 1), but only one of them drives:
  whoever marks it done first. Choosing counts their share of trips they are a potential
  driver on that nobody has driven yet (see `Balance.pending_share`), so they aren't
  chosen twice in a row.
- Ties go to whoever drove longest ago, then by room, so choices are reproducible.
- New members start at score 0 instead of inheriting a backlog.
- A trip nobody drove (missed, excused or disputed) counts for nobody: no share, no
  drive, so its potential drivers still have the lowest scores and come up again soon.
  This matches the paper, where every trip is driven. Strikes do the punishing.
- Penalty trips are outside the count.
- Dishwasher heroes don't ride on kitchen trips during their hero month: the caller passes
  that month in as an away period (`duties.load_members`), so they owe no share for it.
- Moving out: the share of someone who moved out is dropped, not passed on (see
  `charge_riders`), and their score leaves with them. This is the one place the sum can
  differ from 0.

Display: scores are exact fractions, but the statistics page shows them as whole points,
`POINTS_PER_DUTY` per duty, rounded by the largest-remainder method so the shown points
still sum to exactly what the scores sum to (see `display_points`).

Everything in here is pure (no database) so it can be tested and simulated cheaply.
"""

import copy
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

# One duty is shown as this many points.
POINTS_PER_DUTY = 100


@dataclass
class Balance:
    """One member's account in one carpool (duty).

    Fractions instead of floats, so shares like 1/15 add up exactly.
    """

    # Sum of the member's fair shares (1/k) of all driven trips they rode on.
    fair_share: Fraction = Fraction(0)
    # Trips the member drove (1 each).
    drives: Fraction = Fraction(0)
    # Shares of the trips the member is a potential driver on that nobody has driven yet.
    # Counts only for choosing: the next trip is planned before today's is driven, and
    # today's potential drivers shouldn't be chosen again just because they haven't
    # marked it yet.
    pending_share: Fraction = Fraction(0)
    # Start of the latest trip they drove; used to break ties. Being a potential driver
    # without driving doesn't count, so it doesn't move you back in the queue.
    last_drove: date | None = None

    @property
    def score(self) -> Fraction:
        """The paper's score: drives minus fair shares. Changes only when a trip is driven."""
        return self.drives - self.fair_share

    @property
    def effective_score(self) -> Fraction:
        """The score plus pending shares; the lowest are chosen next."""
        return self.score + self.pending_share


@dataclass(frozen=True)
class Member:
    """A member of the carpool (an active user), with the periods they are away."""

    id: int
    room: str
    away: tuple[tuple[date, date], ...] = ()

    def rides(self, start: date, end: date) -> bool:
        """True if no away period overlaps the days `start` to `end` (both included)."""
        return not overlaps(self.away, start, end)


def overlaps(periods: Iterable[tuple[date, date]], start: date, end: date) -> bool:
    """Whether any of `periods` (both ends included) shares a day with `start`..`end`."""
    return any(p_start <= end and start <= p_end for p_start, p_end in periods)


def week_start(d: date) -> date:
    """The Monday of `d`'s week."""
    return d - timedelta(days=d.weekday())


def room_key(room: str) -> tuple:
    """Natural sort: "E1.2" before "E1.10"."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", room))


def charge_riders(balances: dict[int, Balance], riders: Iterable[int | None]) -> None:
    """Charge every rider of one trip their fair share, 1 / number of riders.

    `None` is a rider who has moved out since: they still count for the size of the share,
    but their share is dropped instead of being passed on to the others.
    """
    riders = list(riders)
    if not riders:
        return
    share = Fraction(1, len(riders))
    for uid in riders:
        if uid is not None:
            balances.setdefault(uid, Balance()).fair_share += share


def choose_potential_drivers(
    balances: dict[int, Balance], riders: Iterable[int], n: int, rooms: dict[int, str]
) -> list[int]:
    """Choose the `n` riders with the lowest effective score. Does not modify balances."""

    def key(uid: int):
        b = balances.get(uid, Balance())
        # Lowest score first; on a tie, whoever drove longest ago (never = first), then by
        # room, so the result is always the same for the same input.
        return (b.effective_score, b.last_drove or date.min, room_key(rooms.get(uid, "")), uid)

    return sorted(riders, key=key)[:n]


def credit_driver(balances: dict[int, Balance], uid: int, on: date) -> None:
    """Book one drive for `uid` on the trip starting `on`."""
    b = balances.setdefault(uid, Balance())
    b.drives += 1
    b.last_drove = on if b.last_drove is None else max(b.last_drove, on)


def reserve_share(balances: dict[int, Balance], uid: int, amount: Fraction) -> None:
    """Book `amount` of a trip `uid` is a potential driver on, not driven yet."""
    balances.setdefault(uid, Balance()).pending_share += amount


def display_points(scores: dict[int, Fraction], queue: Sequence[int] = ()) -> dict[int, int]:
    """Scores as whole points (`POINTS_PER_DUTY` per duty) that sum to the rounded total.

    Largest-remainder method: every scaled score is rounded down, then the points still
    missing to the total go to the largest remainders, one each. So every value is less than
    1 point from the exact one, a lower score never shows more points, and with all scores
    summing to 0 the points do too. Equal remainders: whoever comes later in `queue` (the
    order they will be chosen in) gets the point first, so among equal scores whoever is
    next shows fewer points; then by id.
    """
    scaled = {uid: s * POINTS_PER_DUTY for uid, s in scores.items()}
    points = {uid: math.floor(v) for uid, v in scaled.items()}
    missing = round(sum(scaled.values(), Fraction(0))) - sum(points.values())
    position = {uid: i for i, uid in enumerate(queue)}

    def key(uid: int):
        return (points[uid] - scaled[uid], -position.get(uid, -1), uid)

    for uid in sorted(scaled, key=key)[:missing]:
        points[uid] += 1
    return points


def assume_driven(
    balances: dict[int, Balance],
    riders: Iterable[int | None],
    released: dict[int, Fraction],
    driver: int,
    on: date,
) -> dict[int, Balance]:
    """A copy of `balances` as if `driver` drove the open trip starting `on`.

    The trip's `riders` are charged their share, the shares its potential drivers had
    reserved while it was open (`released`, by user id) are taken back, and `driver` gets
    the drive. `balances` itself is not changed.
    """
    balances = copy.deepcopy(balances)
    charge_riders(balances, riders)
    for uid, share in released.items():
        reserve_share(balances, uid, -share)
    credit_driver(balances, driver, on)
    return balances


def plan(
    balances: dict[int, Balance],
    members: list[Member],
    trips: list[tuple[date, date]],
    n: int,
) -> list[list[int]]:
    """Simulate who will be chosen as potential drivers for upcoming trips, for the preview.

    Nobody knows yet who will drive each trip, so every potential driver reserves an equal
    part of it. `balances` is copied, not changed.

    Returns:
        One list of potential drivers (user ids) per entry in `trips`, in order.
    """
    balances = copy.deepcopy(balances)
    rooms = {m.id: m.room for m in members}
    result = []
    for start, end in trips:
        riders = [m.id for m in members if m.rides(start, end)]
        charge_riders(balances, riders)
        chosen = choose_potential_drivers(balances, riders, n, rooms)
        for uid in chosen:
            reserve_share(balances, uid, Fraction(1, len(chosen)))
        result.append(chosen)
    return result
