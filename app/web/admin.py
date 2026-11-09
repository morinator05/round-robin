"""The floor speaker's page and actions: duties, strikes, absences, dishwasher, residents."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated

from fastapi import Form, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.deps import AdminUser, Session
from app.models import AwayPeriod, DishwasherClear, PotentialDriver, Status, Strike, Trip, User
from app.services import dishwasher, duties, jobs, strikes, users
from app.services.errors import UserError
from app.web.common import act, back, get_or_404, parse_id, render, router, start_session

# The admin page shows the duties of this many days, the older half folded away.
DUTY_DAYS = 14


@dataclass
class Attention:
    """What the floor speaker should look at, at the top of the page."""

    # Missed (not disputed, not kept as missed): a strike was given, a decision is needed.
    overdue: list[tuple[Trip, PotentialDriver]]
    # Not marked yet but still in time; a duty that hasn't started yet isn't in here.
    open_now: list[tuple[Trip, PotentialDriver]]
    # Residents one strike or less before a penalty week, with their active strikes.
    near: list[tuple[User, int]]

    def __len__(self) -> int:
        return len(self.overdue) + len(self.open_now) + len(self.near)


def attention(trips: list[Trip], active: list[Strike], residents: list[User], today: date):
    """The `Attention` list for these trips and active strikes."""
    overdue, open_now = [], []
    for trip in trips:
        for pd in trip.potential_drivers:
            if pd.status is Status.missed and not pd.disputed_at and not pd.kept_missed_at:
                overdue.append((trip, pd))
            elif pd.status is Status.pending and trip.start_date <= today:
                open_now.append((trip, pd))
    counts = {u.id: sum(s.user_id == u.id for s in active) for u in residents}
    near = [(u, counts[u.id]) for u in residents if strikes.near_penalty(counts[u.id])]
    return Attention(overdue, open_now, near)


@router.get("/admin")
async def admin(request: Request, session: Session, user: AdminUser):
    """The floor speaker's page."""
    now = settings.now()
    today = now.date()
    residents = await users.list_users(session)
    trips = list(
        await session.scalars(
            select(Trip)
            .where(Trip.end_date >= today - timedelta(days=DUTY_DAYS))
            .options(selectinload(Trip.potential_drivers).selectinload(PotentialDriver.user))
            .order_by(Trip.start_date.desc(), Trip.duty)
        )
    )
    all_strikes = list(
        await session.scalars(
            select(Strike)
            .options(selectinload(Strike.user), selectinload(Strike.issued_by))
            .order_by(Strike.created_at.desc())
            .limit(50)
        )
    )
    active = [s for s in all_strikes if s.is_active]
    # Current and upcoming absences of everyone, so unusual ones are noticed.
    aways = await session.scalars(
        select(AwayPeriod)
        .where(AwayPeriod.end_date >= today)
        .options(selectinload(AwayPeriod.user))
        .order_by(AwayPeriod.start_date, AwayPeriod.id)
    )
    open_statuses = (Status.pending, Status.missed)
    return render(
        request,
        "admin/index.html",
        user,
        users=residents,
        trips=trips,
        open_duties=sum(pd.status in open_statuses for t in trips for pd in t.potential_drivers),
        # Duties that ended before this are folded away under "earlier days".
        fold_before=today - timedelta(days=DUTY_DAYS // 2),
        attention=attention(trips, active, residents, today),
        active_strikes=active,
        old_strikes=[s for s in all_strikes if not s.is_active],
        aways=list(aways),
        dishwasher_clears=await dishwasher.clears_in_month(session, today),
        can_mark=lambda pd: duties.can_mark_as_driver(pd, user, now),
        can_undo=lambda pd: duties.can_undo(pd, user, now),
    )


@router.post("/admin/users")
async def admin_create_user(
    request: Request,
    session: Session,
    user: AdminUser,
    name: Annotated[str, Form()],
    room_number: Annotated[str, Form()],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    is_admin: Annotated[bool, Form()] = False,
):
    """Add a resident and put them on today's duties if people are missing."""

    async def create():
        new = await users.create(
            session,
            name=name,
            room_number=room_number,
            username=username,
            password=password,
            is_admin=is_admin,
        )
        await duties.fill_current(session, settings.now())
        return f"{new.display_name} added"

    return await act(request, session, create, "/admin")


@router.post("/admin/users/{user_id}/edit")
async def admin_edit_user(
    request: Request,
    session: Session,
    user: AdminUser,
    user_id: int,
    name: Annotated[str, Form()],
    room_number: Annotated[str, Form()],
    password: Annotated[str, Form()] = "",
    is_admin: Annotated[bool, Form()] = False,
):
    """Change a resident's name, room, password or admin role (not your own role)."""
    target = await get_or_404(session, User, user_id)

    async def edit():
        await users.update(
            session,
            target,
            user,
            name=name,
            room_number=room_number,
            password=password,
            is_admin=is_admin,
        )
        if target.id == user.id:
            start_session(request, user)  # your own new password: stay logged in here
        return f"{target.display_name} saved"

    return await act(request, session, edit, "/admin")


@router.post("/admin/users/{user_id}/active")
async def admin_toggle_active(request: Request, session: Session, user: AdminUser, user_id: int):
    """Pause or resume a resident."""
    target = await get_or_404(session, User, user_id)

    async def toggle():
        await users.set_active(session, target, not target.active, settings.now())
        return f"{target.display_name} is now {'active' if target.active else 'inactive'}"

    return await act(request, session, toggle, "/admin")


@router.post("/admin/users/{user_id}/delete")
async def admin_delete_user(request: Request, session: Session, user: AdminUser, user_id: int):
    """Delete a resident who moved out (not yourself)."""
    target = await get_or_404(session, User, user_id)

    async def delete():
        if target.id == user.id:
            raise UserError("You can't delete yourself")
        name = target.display_name
        await users.delete(session, target, settings.now())
        return f"{name} deleted"

    return await act(request, session, delete, "/admin")


async def _resident(session, user_id: str, missing: str) -> User:
    """The resident chosen in a form's select; `missing` says what to choose if nobody is."""
    if (uid := parse_id(user_id)) is None:
        raise UserError(missing)
    return await get_or_404(session, User, uid)


@router.post("/admin/strikes")
async def admin_issue_strike(
    request: Request,
    session: Session,
    user: AdminUser,
    user_id: Annotated[str, Form()],
    reason: Annotated[str, Form()],
):
    """Give a strike by hand."""

    async def issue():
        target = await _resident(session, user_id, "Choose who gets the strike")
        await strikes.issue(
            session,
            user_id=target.id,
            reason=reason.strip() or "–",
            issued_by_id=user.id,
            today=settings.today(),
        )
        return f"Strike given to {target.display_name}"

    return await act(request, session, issue, "/admin")


@router.post("/admin/strikes/{strike_id}/revoke")
async def admin_revoke_strike(request: Request, session: Session, user: AdminUser, strike_id: int):
    """Take a strike back."""
    strike = await get_or_404(session, Strike, strike_id)

    async def revoke():
        await strikes.revoke(session, strike, settings.now())
        return "Strike revoked"

    return await act(request, session, revoke, "/admin")


@router.post("/admin/aways")
async def admin_add_away(
    request: Request,
    session: Session,
    user: AdminUser,
    user_id: Annotated[str, Form()],
    start_date: Annotated[date, Form()],
    end_date: Annotated[date, Form()],
):
    """Enter an away period for a resident (who told the floor speaker in person)."""

    async def add():
        target = await _resident(session, user_id, "Choose who is away")
        await users.add_away(session, target, start_date, end_date, settings.now())
        return f"Away period added for {target.display_name}"

    return await act(request, session, add, "/admin")


@router.post("/admin/aways/{away_id}/delete")
async def admin_delete_away(request: Request, session: Session, user: AdminUser, away_id: int):
    """Delete anyone's away period."""
    away = await get_or_404(session, AwayPeriod, away_id)

    async def delete():
        await users.remove_away(session, away)
        return "Away period deleted"

    return await act(request, session, delete, "/admin")


async def _on_duty(request, session, user, potential_driver_id, action, done, to="/admin"):
    """`act` on one potential driver with a floor speaker's `action` (`duties.excuse`, …)."""

    async def run():
        pd = await duties.get_potential_driver(session, potential_driver_id)
        await action(session, pd, user, settings.now())
        return done(pd) if callable(done) else done

    return await act(request, session, run, to)


@router.post("/admin/potential-drivers/{potential_driver_id}/excuse")
async def admin_excuse(
    request: Request, session: Session, user: AdminUser, potential_driver_id: int
):
    """Excuse a duty: no strike, no drive."""
    return await _on_duty(
        request, session, user, potential_driver_id, duties.excuse, "Duty excused"
    )


@router.post("/admin/potential-drivers/{potential_driver_id}/keep-missed")
async def admin_keep_missed(
    request: Request, session: Session, user: AdminUser, potential_driver_id: int
):
    """Leave a missed duty as it is (the strike stays) and take it off "Needs attention"."""
    done = "Kept as missed, the strike stays"
    return await _on_duty(request, session, user, potential_driver_id, duties.keep_missed, done)


@router.post("/admin/potential-drivers/{potential_driver_id}/dispute")
async def admin_dispute(
    request: Request, session: Session, user: AdminUser, potential_driver_id: int
):
    """Marked as done but not actually done: the driver gets a strike."""

    def done(pd: PotentialDriver) -> str:
        return (
            f"Disputed, {pd.user.display_name if pd.user else 'the former resident'} gets a strike"
        )

    to = back(request, "/history")
    return await _on_duty(request, session, user, potential_driver_id, duties.dispute, done, to)


@router.post("/admin/dishwasher/{clear_id}/delete")
async def admin_delete_dishwasher(
    request: Request, session: Session, user: AdminUser, clear_id: int
):
    """Remove a wrongly marked emptying of the dishwasher (current month only)."""
    clear = await get_or_404(session, DishwasherClear, clear_id)

    async def delete():
        await dishwasher.delete_clear(session, clear, user, settings.now())
        return "Dishwasher entry deleted"

    return await act(request, session, delete, "/admin")


@router.post("/admin/tick")
async def admin_tick(request: Request, session: Session, user: AdminUser):
    """Run the daily job now."""

    async def tick():
        await jobs.daily_tick(session, settings.now())
        return "Daily job done"

    return await act(request, session, tick, "/admin")
