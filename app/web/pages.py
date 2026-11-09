"""The pages to look things up: history, statistics and help."""

from datetime import date, timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.deps import CurrentUser, Session
from app.models import Duty, PotentialDriver, Trip
from app.services import dishwasher, duties, strikes, users
from app.web.common import render, router

HISTORY_DAYS = 28


@router.get("/history")
async def history(request: Request, session: Session, user: CurrentUser, until: str | None = None):
    """Who drove which trip, `HISTORY_DAYS` days up to `until` (lenient: an empty or invalid
    date shows the latest days)."""
    today = settings.today()
    try:
        day = date.fromisoformat(until) if until else today
    except ValueError:
        day = today
    until = max(min(day, today), date(2000, 1, 1))
    since = until - timedelta(days=HISTORY_DAYS - 1)
    trips = await session.scalars(
        select(Trip)
        .where(Trip.start_date <= until, Trip.end_date >= since)
        .options(
            selectinload(Trip.potential_drivers).selectinload(PotentialDriver.user),
            selectinload(Trip.potential_drivers).selectinload(PotentialDriver.completed_by),
        )
        .order_by(Trip.start_date.desc(), Trip.duty)
    )
    return render(
        request,
        "history.html",
        user,
        trips=list(trips),
        since=since,
        until=until,
        older=since - timedelta(days=1),
        newer=min(until + timedelta(days=HISTORY_DAYS), today) if until < today else None,
    )


@router.get("/stats")
async def stats(request: Request, session: Session, user: CurrentUser):
    """Per resident: points, strikes, duties done and missed; the dishwasher count."""
    all_users = await users.list_users(session)
    points = await duties.points(session, all_users)
    rows = (
        await session.execute(
            select(
                PotentialDriver.user_id, Trip.duty, Trip.is_penalty, PotentialDriver.status
            ).join(PotentialDriver.trip)
        )
    ).all()
    # (user id, duty, status) -> number of trips; penalty weeks count as "penalty".
    counts: dict[tuple, int] = {}
    for uid, duty, is_penalty, status in rows:
        key = (uid, duty, "penalty" if is_penalty else status)
        counts[key] = counts.get(key, 0) + 1
    active_counts = await strikes.active_counts(session)
    active = {u.id: active_counts.get(u.id, 0) for u in all_users}
    today = settings.today()
    by_id = {u.id: u for u in all_users}
    return render(
        request,
        "stats.html",
        user,
        users=all_users,
        points=points,
        counts=counts,
        active=active,
        dishwasher_rows=[
            (by_id[s.user_id], s.count)
            for s in await dishwasher.standings(session, today)
            if s.user_id in by_id
        ],
        heroes_month=dishwasher.next_month(today),
        you={
            "points": {d: points[d].get(user.id, 0) for d in Duty},
            "strikes": active.get(user.id, 0),
        },
    )


@router.get("/help")
async def help_page(request: Request, session: Session, user: CurrentUser):
    """How the duties work, for new residents."""
    return render(
        request,
        "help.html",
        user,
        floor_speakers=[u for u in await users.list_users(session) if u.is_admin],
        kitchen_people=settings.kitchen_people_per_day,
        trash_people=settings.trash_people_per_week,
        preview_days=settings.preview_days,
    )
