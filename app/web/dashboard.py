"""The dashboard: today's duties, the dishwasher, and the preview; and their buttons."""

from datetime import date, datetime, timedelta

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import CurrentUser, Session
from app.models import DishwasherClear, Duty, PotentialDriver, Status, Trip, User
from app.services import dishwasher, duties, jobs, strikes
from app.services.errors import DishwasherError
from app.web.common import act, back, get_row, render, router, users_by_id


@router.get("/")
async def dashboard(
    request: Request, session: Session, user: CurrentUser, cleaner: str | None = None
):
    """Today's duties and the preview.

    While nobody has driven the open kitchen trip, the preview has one plan per potential
    driver; `cleaner` picks which one is shown (default: yourself if you're one of them,
    else the first).
    """
    now = settings.now()
    today = now.date()
    # Before the deadline hour that's still yesterday's (last week's) duty.
    current = await jobs.current_trips(session, now)
    kitchen, trash = current[Duty.kitchen], current[Duty.trash]

    def after(trip: Trip | None) -> date:
        """The preview starts with the first trip after the one on the card."""
        return trip.end_date + timedelta(days=1) if trip else today

    horizon = today + timedelta(days=settings.preview_days)
    kitchen_preview = await duties.preview(session, Duty.kitchen, after(kitchen), horizon, today)
    trash_preview = await duties.preview(session, Duty.trash, after(trash), horizon, today)
    people = await users_by_id(session)

    choices = [uid for uid in kitchen_preview.plans if uid is not None]
    # Lenient: anything that isn't one of the choices (also "abc") shows the default.
    try:
        chosen = int(cleaner) if cleaner and cleaner.isascii() else None
    except ValueError:
        chosen = None
    if chosen not in kitchen_preview.plans:
        chosen = user.id if user.id in choices else next(iter(kitchen_preview.plans))
    kitchen_plan = kitchen_preview.plans[chosen]
    # The open trip's potential drivers who aren't up again within the plan (usually
    # whoever cleans): said below it, so nobody wonders where they went.
    shown = {uid for e in kitchen_plan for uid in e.potential_drivers}
    later = [people[uid] for uid in choices if uid not in shown and uid in people]
    trash_plan = next(iter(trash_preview.plans.values()))
    return render(
        request,
        "dashboard.html",
        user,
        you=await you_ctx(session, user, now, kitchen, trash, kitchen_preview, trash_plan, people),
        kitchen=duty_card_ctx(kitchen, Duty.kitchen, user, now),
        trash=duty_card_ctx(trash, Duty.trash, user, now),
        kitchen_plan=kitchen_plan,
        kitchen_choices=[people[uid] for uid in choices if uid in people],
        kitchen_cleaner=chosen,
        kitchen_open_start=kitchen_preview.open_start,
        yesterday=today - timedelta(days=1),
        kitchen_later=later,
        trash_plan=trash_plan,
        preview_days=settings.preview_days,
        users_by_id=people,
        dishwasher=await dishwasher_ctx(session, user, now, people),
    )


def duty_card_ctx(trip: Trip | None, duty: Duty, user: User, now: datetime) -> dict:
    """Template variables for `partials/duty_card.html`.

    `can_mark_as_driver` and `can_undo` are functions, so the template can ask for each
    potential driver on the card what the logged-in user may do.
    """
    if trip is None or trip.start_date == now.date():
        day = "today"
    elif trip.start_date == now.date() - timedelta(days=1):
        day = "yesterday"  # before the deadline hour the card still shows yesterday's
    else:
        day = None
    return {
        "trip": trip,
        "duty": duty,
        "day": day,
        "deadline": duties.deadline(trip) if trip else None,
        "can_mark_as_driver": lambda pd: duties.can_mark_as_driver(pd, user, now),
        "can_undo": lambda pd: duties.can_undo(pd, user, now),
    }


def _next_in(plan: list[duties.PreviewEntry], user_id: int) -> duties.PreviewEntry | None:
    """The user's first trip in a preview plan, if they are in it."""
    return next((e for e in plan if user_id in e.potential_drivers), None)


async def you_ctx(
    session: AsyncSession,
    user: User,
    now: datetime,
    kitchen: Trip | None,
    trash: Trip | None,
    kitchen_preview: duties.Preview,
    trash_plan: list[duties.PreviewEntry],
    people: dict[int, User],
) -> dict:
    """The "you" line at the top of the dashboard: your duties now, your next ones, your
    strikes, and a pointer to the help for someone who has never had a duty."""

    def has(trip: Trip | None, status: Status) -> bool:
        """Whether the user is on `trip` with `status`."""
        return trip is not None and any(
            pd.user_id == user.id and pd.status is status for pd in trip.potential_drivers
        )

    # Your next kitchen day for each possible outcome of the open trip (who cleans it),
    # earliest first; outcomes giving the same day are one. With a single outcome it also
    # names who you'll be on it with.
    outcomes: list[dict] = []
    for uid, plan in kitchen_preview.plans.items():
        entry = _next_in(plan, user.id)
        start = entry.start if entry else None
        if any(o["start"] == start for o in outcomes):
            continue
        partners = [people.get(p) for p in (entry.potential_drivers if entry else [])]
        outcomes.append(
            {
                "entry": entry,
                "start": start,
                "cleaner": people.get(uid) if uid is not None else None,
                "is_me": uid == user.id,
                "partners": [p for p in partners if p is not None and p.id != user.id],
            }
        )
    outcomes.sort(key=lambda o: o["start"] or date.max)
    duty_count = await session.scalar(
        select(func.count(PotentialDriver.id)).where(PotentialDriver.user_id == user.id)
    )
    today = now.date()
    return {
        "inactive": not user.active,
        "kitchen_now": has(kitchen, Status.pending),
        "kitchen_done": has(kitchen, Status.drove),
        # Before the deadline hour the cards show yesterday's / last week's duty.
        "kitchen_late": kitchen is not None and kitchen.end_date < today,
        "kitchen_deadline": duties.deadline(kitchen) if kitchen else None,
        "trash_late": trash is not None and trash.end_date < today,
        "trash_deadline": duties.deadline(trash) if trash else None,
        "trash_now": has(trash, Status.pending),
        "trash_done": has(trash, Status.drove),
        "kitchen_outcomes": outcomes,
        "trash_next": _next_in(trash_plan, user.id),
        "strikes": len(await strikes.active_strikes(session, user.id)),
        "is_new": not duty_count,
    }


async def dishwasher_ctx(
    session: AsyncSession, user: User, now: datetime, people: dict[int, User]
) -> dict:
    """Template variables for `partials/dishwasher_card.html` and the hero line above the
    kitchen preview.

    `state` is what the user's row shows: "can_mark" (the button), "done" (they emptied it
    last and the next one doesn't count yet), "not_needed" (someone else did) or "inactive".
    """
    last = await dishwasher.last_clear(session)
    allowed = dishwasher.next_allowed(last)
    waiting = allowed is not None and now < allowed
    if not user.active:
        state = "inactive"
    elif not waiting:
        state = "can_mark"
    elif last.user_id == user.id:
        state = "done"
    else:
        state = "not_needed"
    summary = await dishwasher.summary_for(session, user, now.date())
    heroes = [people[uid] for uid in summary.heroes if uid in people]
    return {
        "month": now.date(),
        "last": last,
        "next_allowed": allowed if waiting else None,
        "can_undo": last is not None and dishwasher.can_undo_clear(last, user, now),
        "state": state,
        "mine": summary.mine,
        "heroes": heroes,
        "is_hero": summary.is_hero(user.id),
        "other_heroes": [u for u in heroes if u.id != user.id],
        "heroes_from": summary.heroes_from,
    }


@router.post("/dishwasher")
async def mark_dishwasher(request: Request, session: Session, user: CurrentUser):
    """You emptied the dishwasher; from the dashboard card."""

    async def mark():
        await dishwasher.mark_cleared(session, user, settings.now())
        return "Marked as emptied"

    return await act(request, session, mark)


@router.post("/dishwasher/{clear_id}/undo")
async def undo_dishwasher(request: Request, session: Session, user: CurrentUser, clear_id: int):
    """Take back your own mis-tapped emptying; from the dashboard card."""

    async def undo():
        clear = await get_row(session, DishwasherClear, clear_id)
        if clear is None:
            raise DishwasherError("That entry doesn't exist any more")
        await dishwasher.undo_clear(session, clear, user, settings.now())
        return "Taken back"

    return await act(request, session, undo)


async def on_duty(request: Request, session, user: User, potential_driver_id: int, action, done):
    """`act` on one potential driver with `action` (`duties.mark_as_driver` and the like)."""

    async def run():
        pd = await duties.get_potential_driver(session, potential_driver_id)
        await action(session, pd, user, settings.now())
        return done

    return await act(request, session, run, back(request))


@router.post("/potential-drivers/{potential_driver_id}/drove")
async def mark_as_driver(
    request: Request, session: Session, user: CurrentUser, potential_driver_id: int
):
    """Mark who drove (did the duty), from the dashboard card or the admin page."""
    return await on_duty(
        request, session, user, potential_driver_id, duties.mark_as_driver, "Marked as done"
    )


@router.post("/potential-drivers/{potential_driver_id}/undo")
async def undo_driver(
    request: Request, session: Session, user: CurrentUser, potential_driver_id: int
):
    """Take back a mis-tapped "Done", from the dashboard card or the admin page."""
    done = "Undone: the duty is open again"
    return await on_duty(request, session, user, potential_driver_id, duties.undo_driver, done)
