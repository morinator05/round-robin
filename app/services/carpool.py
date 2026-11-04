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
- Moving out: the share of someone who moved out is dropped, not passed on (see
  `charge_riders`), and their score leaves with them. This is the one place the sum can
  differ from 0.

The preview can't know which potential driver of an undriven trip will drive it, so it
replaces them by `Role`s: the one who drives (with the drive) and the ones who don't.

Everything in here is pure (no database) so it can be tested and simulated cheaply.
"""

import copy
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction


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
        """True if the member rides on a trip from `start` to `end` (both included).

        That is, no away period overlaps those days.
        """
        return not any(a_start <= end and start <= a_end for a_start, a_end in self.away)


@dataclass(frozen=True)
class Role:
    """Preview stand-in for the potential drivers of a trip nobody has driven yet.

    Shows where each outcome (drove / didn't drive) puts them, instead of guessing a name.
    """

    # First day of the undriven trip.
    start: date
    drove: bool
    # The trip's real potential drivers (any of them can end up in this role).
    potential_drivers: tuple[int, ...] = ()


def week_start(d: date) -> date:
    """The Monday of `d`'s week."""
    return d - timedelta(days=d.weekday())


def room_key(room: str) -> tuple:
    """Natural sort: "A4.2" before "A4.10"."""
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


def _replace_by_roles(
    balances: dict[int, Balance],
    members: list[Member],
    start: date,
    potential_driver_ids: Iterable[int],
    roles: dict[int, Role],
) -> list[Member]:
    """Swap an undriven trip's potential drivers for one driver and the others as non-drivers.

    Each role gets the potential drivers' average balance as it will be after the trip:
    the driver with the drive (and the trip as last drove), the others unchanged, nothing
    pending. A role is away whenever any of them is, since it could be any of them. Roles
    get negative ids, added to `roles`. Changes `balances` in place and returns the new
    members.
    """
    ids = set(potential_driver_ids)
    drivers = [m for m in members if m.id in ids]
    if len(drivers) < 2:
        return members
    before = [balances.pop(m.id, Balance()) for m in drivers]
    fair_share = sum((b.fair_share for b in before), Fraction(0)) / len(before)
    drives = sum((b.drives for b in before), Fraction(0)) / len(before)
    last_drove = max((b.last_drove for b in before if b.last_drove), default=None)
    room = min((m.room for m in drivers), key=room_key)
    away = tuple(period for m in drivers for period in m.away)
    driver_tuple = tuple(sorted(m.id for m in drivers))

    result = [m for m in members if m.id not in ids]
    for drove in [True] + [False] * (len(drivers) - 1):
        rid = -(len(roles) + 1)
        roles[rid] = Role(start, drove, driver_tuple)
        balances[rid] = Balance(
            fair_share=fair_share,
            drives=drives + (1 if drove else 0),
            last_drove=start if drove else last_drove,
        )
        result.append(Member(rid, room, away))
    return result


def plan(
    balances: dict[int, Balance],
    members: list[Member],
    trips: list[tuple[date, date]],
    n: int,
    undriven_trips: Iterable[tuple[date, list[int]]] = (),
) -> list[list[int | Role]]:
    """Simulate who will be chosen as potential drivers for upcoming trips, for the preview.

    Nobody knows yet who will drive each trip, so every potential driver reserves an equal
    part of it. `balances` is copied, not changed.

    `undriven_trips` are the real trips nobody has driven yet, as (first day, potential
    drivers). They are replaced by `Role`s, so the preview shows when they're up again if
    they drive and if they don't. A role is shown only once: after that it's a person whose
    next outcome is unknown again, so it leaves the simulation.

    Returns:
        One list of potential drivers (user ids or `Role`s) per entry in `trips`, in order.
    """
    balances = copy.deepcopy(balances)
    roles: dict[int, Role] = {}
    for start, driver_ids in undriven_trips:
        members = _replace_by_roles(balances, members, start, driver_ids, roles)
    rooms = {m.id: m.room for m in members}
    result = []
    for start, end in trips:
        riders = [m.id for m in members if m.rides(start, end)]
        charge_riders(balances, riders)
        chosen = choose_potential_drivers(balances, riders, n, rooms)
        for uid in chosen:
            reserve_share(balances, uid, Fraction(1, len(chosen)))
        result.append([roles.get(uid, uid) for uid in chosen])
        members = [m for m in members if m.id not in roles or m.id not in chosen]
    return result
