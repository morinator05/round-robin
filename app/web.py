"""All pages and form actions (server-rendered Jinja templates, htmx for small updates).

Routes only parse input, call a service and render or redirect; the rules live in
`app/services/`. Every form POST answers with a redirect (303), so reloading the page
doesn't submit the form again.
"""

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.deps import AdminUser, CurrentUser, OptionalUser, Session
from app.models import AwayPeriod, Duty, PotentialDriver, Status, Strike, Trip, User
from app.services import auth, duties, jobs, strikes, users
from app.services.carpool import Role
from app.services.duties import DutyError
from app.services.login_throttle import login_throttle
from app.services.users import UserError

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def person_html(user: User | None, is_me: bool = False) -> Markup:
    """A person as HTML: the first name stands out, the room is a quieter tag after it.

    `is_me` highlights the logged-in user's own entry, so they find themselves quickly.
    """
    if user is None:
        return Markup('<span class="muted">ehemalig</span>')
    html = Markup('<span class="person-name">{}</span> <span class="room">{}</span>').format(
        user.first_name, user.room_number
    )
    if is_me:
        return Markup('<span class="me">{}</span>').format(html)
    return html


@pass_context
def person(context, user: User | None) -> Markup:
    """`person_html` for templates: takes the logged-in user from the page's `user`."""
    viewer = context.get("user")
    is_me = user is not None and viewer is not None and user.id == viewer.id
    return person_html(user, is_me)


@pass_context
def role(context, r: Role) -> Markup:
    """A preview stand-in: "Erledigt" (whoever drives) or "Ausgelassen" (whoever doesn't).

    For an earlier trip nobody has driven yet (yesterday's before the deadline), its date
    follows. Green with a tick if driven, amber with a cross if not.
    """
    text = "Erledigt" if r.drove else "Ausgelassen"
    if r.start != context.get("today"):
        text += f" {r.start:%d.%m.}"
    css = "role role-done" if r.drove else "role role-skip"
    return Markup('<span class="{}"><span aria-hidden="true">{}</span> {}</span>').format(
        css, "✓" if r.drove else "✗", text
    )


def help_link(topic: str) -> Markup:
    """A small "?" after a heading, linking to that topic on the help page."""
    return Markup(
        '<a class="help-link" href="/help#{}" title="Wie funktioniert das?" '
        'aria-label="Wie funktioniert das?">?</a>'
    ).format(topic)


templates.env.globals["person"] = person
templates.env.globals["help_link"] = help_link
templates.env.globals["role"] = role
templates.env.tests["role"] = lambda x: isinstance(x, Role)
templates.env.globals["Duty"] = Duty
templates.env.globals["Status"] = Status
templates.env.globals["strike_threshold"] = settings.strike_threshold
templates.env.globals["min_password_length"] = auth.MIN_PASSWORD_LENGTH
# Appended to the stylesheet URL, so browsers load it again after it changed instead of
# keeping an old copy from their cache.
templates.env.globals["css_version"] = int(
    (Path(__file__).parent / "static" / "style.css").stat().st_mtime
)
templates.env.filters["short_date"] = lambda d: f"{d:%d.%m.}" if d else ""
templates.env.filters["date_time"] = lambda d: (
    f"{d.astimezone(settings.tz):%d.%m.%Y %H:%M}" if d else ""
)
templates.env.filters["weekday"] = lambda d: ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][d.weekday()]


def flash(request: Request, message: str, kind: str = "ok") -> None:
    """Queue a message for the next rendered page. `kind` is "ok" or "error"."""
    # Assign a new list: the session only notices assignments, not in-place changes.
    request.session["flash"] = [*request.session.get("flash", []), [kind, message]]


def render(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    """Render a full page; adds the user, pending messages and today's date."""
    messages = request.session.pop("flash", [])
    return templates.TemplateResponse(
        request, name, {"user": user, "messages": messages, "today": settings.today(), **ctx}
    )


def redirect(url: str) -> RedirectResponse:
    """Redirect after a form POST; 303 makes the browser follow it with a GET."""
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None) -> str:
    """`url` if it's a path on this site, else "/". Stops redirects to other sites."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


async def users_by_id(session: AsyncSession) -> dict[int, User]:
    """All users by id, to show the names in the preview."""
    return {u.id: u for u in await session.scalars(select(User))}


def duty_card_ctx(trip: Trip | None, duty: Duty, user: User | None) -> dict:
    """Template variables for `partials/duty_card.html`.

    `can_mark_as_driver` is a function, so the template can ask for each potential driver
    on the card whether the logged-in user may mark them as the driver.
    """
    now = settings.now()
    return {
        "trip": trip,
        "duty": duty,
        "deadline": duties.deadline(trip) if trip else None,
        "can_mark_as_driver": (
            lambda pd: user is not None and duties.can_mark_as_driver(pd, user, now)
        ),
    }


# --- auth -------------------------------------------------------------------


@router.get("/login")
async def login_page(request: Request, user: OptionalUser, next: str = "/"):
    """Login form; `next` is where to go after logging in."""
    if user:
        return redirect(safe_next(next))
    return render(request, "login.html", None, next=next)


@router.post("/login")
async def login(
    request: Request,
    session: Session,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    """Log in. Too many failed attempts block the username from that IP for a while."""
    client_ip = request.client.host if request.client else "unknown"
    throttle_key = f"{username.strip().lower()}@{client_ip}"
    if login_throttle.is_blocked(throttle_key):
        flash(request, "Zu viele Fehlversuche, bitte in 15 Minuten nochmal versuchen", "error")
        return redirect(f"/login?next={safe_next(next)}")

    user = await auth.authenticate(session, username, password)
    if user is None:
        login_throttle.record_failure(throttle_key)
        flash(request, "Benutzername oder Passwort falsch", "error")
        return redirect(f"/login?next={safe_next(next)}")
    login_throttle.reset(throttle_key)
    request.session["user_id"] = user.id
    return redirect(safe_next(next))


@router.post("/logout")
async def logout(request: Request):
    """Log out."""
    request.session.clear()
    return redirect("/")


# --- dashboard --------------------------------------------------------------


@router.get("/")
async def dashboard(request: Request, session: Session, user: OptionalUser):
    """Today's duties and the preview; visible without login."""
    today = settings.today()
    kitchen = await duties.get_trip(session, Duty.kitchen, today)
    trash = await duties.get_trip(session, Duty.trash, today)
    if kitchen is None or trash is None:
        await jobs.daily_tick(session, settings.now())
        kitchen = await duties.get_trip(session, Duty.kitchen, today)
        trash = await duties.get_trip(session, Duty.trash, today)

    tomorrow = today + timedelta(days=1)
    horizon = today + timedelta(days=settings.preview_days)
    kitchen_preview = await duties.preview(session, Duty.kitchen, tomorrow, horizon)
    shown = [u for e in kitchen_preview for u in e.potential_drivers if isinstance(u, Role)]
    # Whoever drives today's trip is usually up only after the plan ends; say so below it.
    later = [
        r
        for r in dict.fromkeys(replace(u, drove=True) for u in shown if not u.drove)
        if r not in shown
    ]
    return render(
        request,
        "dashboard.html",
        user,
        kitchen=duty_card_ctx(kitchen, Duty.kitchen, user),
        trash=duty_card_ctx(trash, Duty.trash, user),
        kitchen_preview=kitchen_preview,
        kitchen_later=later,
        trash_preview=await duties.preview(session, Duty.trash, tomorrow, horizon),
        users_by_id=await users_by_id(session),
    )


@router.post("/potential-drivers/{potential_driver_id}/drove")
async def mark_as_driver(
    request: Request, session: Session, user: CurrentUser, potential_driver_id: int
):
    """Mark who drove (did the duty), from the dashboard card or the Flursprecher page."""
    # The dashboard card posts with `card=1` and swaps only itself with the response.
    is_card_request = bool(request.headers.get("HX-Request") and request.query_params.get("card"))
    try:
        pd = await duties.get_potential_driver(session, potential_driver_id)
        await duties.mark_as_driver(session, pd, user, settings.now())
        await session.commit()
    except DutyError as e:
        flash(request, str(e), "error")
        if is_card_request:
            # A redirect would put the whole page into the card; reload so the flash shows.
            return Response(headers={"HX-Refresh": "true"})
        return redirect(safe_next(request.query_params.get("next")))
    if is_card_request:
        trip = await duties.get_trip(session, pd.trip.duty, pd.trip.start_date)
        return templates.TemplateResponse(
            request,
            "partials/duty_card.html",
            {**duty_card_ctx(trip, trip.duty, user), "user": user},
        )
    flash(request, "Als erledigt eingetragen")
    return redirect(safe_next(request.query_params.get("next")))


# --- personal page ----------------------------------------------------------


@router.get("/me")
async def me(request: Request, session: Session, user: CurrentUser):
    """Own page: away periods, password, strikes and recent duties."""
    aways = await session.scalars(
        select(AwayPeriod)
        .where(AwayPeriod.user_id == user.id, AwayPeriod.end_date >= settings.today())
        .order_by(AwayPeriod.start_date)
    )
    my_strikes = await session.scalars(
        select(Strike).where(Strike.user_id == user.id).order_by(Strike.created_at.desc())
    )
    history = await session.scalars(
        select(PotentialDriver)
        .join(PotentialDriver.trip)
        .where(PotentialDriver.user_id == user.id)
        .options(selectinload(PotentialDriver.trip))
        .order_by(Trip.start_date.desc())
        .limit(30)
    )
    return render(
        request,
        "me.html",
        user,
        aways=list(aways),
        strikes=list(my_strikes),
        history=list(history),
    )


@router.post("/me/away")
async def add_away(
    request: Request,
    session: Session,
    user: CurrentUser,
    start_date: Annotated[date, Form()],
    end_date: Annotated[date, Form()],
):
    """Add an away period for yourself."""
    try:
        await users.add_away(session, user, start_date, end_date, settings.today())
        await session.commit()
        flash(request, "Abwesenheit eingetragen")
    except UserError as e:
        flash(request, str(e), "error")
    return redirect("/me")


@router.post("/me/away/{away_id}/delete")
async def delete_away(request: Request, session: Session, user: CurrentUser, away_id: int):
    """Delete an away period (your own, or anyone's as Flursprecher)."""
    away = await session.get(AwayPeriod, away_id)
    if away is None or (away.user_id != user.id and not user.is_admin):
        raise HTTPException(404)
    await users.remove_away(session, away)
    await session.commit()
    flash(request, "Abwesenheit gelöscht")
    return redirect("/me")


@router.post("/me/password")
async def change_password(
    request: Request,
    session: Session,
    user: CurrentUser,
    old_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
):
    """Change your own password."""
    if not auth.verify_password(old_password, user.password_hash):
        flash(request, "Altes Passwort falsch", "error")
    elif len(new_password) < auth.MIN_PASSWORD_LENGTH:
        flash(request, auth.PASSWORD_TOO_SHORT, "error")
    else:
        user.password_hash = auth.hash_password(new_password)
        await session.commit()
        flash(request, "Passwort geändert")
    return redirect("/me")


# --- history ----------------------------------------------------------------

HISTORY_DAYS = 28


@router.get("/history")
async def history(request: Request, session: Session, user: CurrentUser, until: date | None = None):
    """Who drove which trip, `HISTORY_DAYS` days up to `until`."""
    today = settings.today()
    until = min(until or today, today)
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


# --- stats ------------------------------------------------------------------


@router.get("/stats")
async def stats(request: Request, session: Session, user: OptionalUser):
    """Per resident: duties driven and missed, score and strikes; visible without login."""
    all_users = await users.list_users(session)
    balances = {d: await duties.load_balances(session, d) for d in Duty}
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
    active = {u.id: len(await strikes.active_strikes(session, u.id)) for u in all_users}
    return render(
        request,
        "stats.html",
        user,
        users=all_users,
        balances=balances,
        counts=counts,
        active=active,
    )


# --- admin ------------------------------------------------------------------


@router.get("/help")
async def help_page(request: Request, user: OptionalUser):
    """How the duties work, for new residents; visible without login."""
    return render(
        request,
        "help.html",
        user,
        kitchen_people=settings.kitchen_people_per_day,
        trash_people=settings.trash_people_per_week,
        deadline_hour=settings.deadline_hour,
        deadline_days_after=settings.deadline_days_after,
        preview_days=settings.preview_days,
    )


@router.get("/admin")
async def admin(request: Request, session: Session, user: AdminUser):
    """Flursprecher page: residents, strikes and recent duties."""
    all_users = await users.list_users(session)
    since = settings.today() - timedelta(days=14)
    trips = await session.scalars(
        select(Trip)
        .where(Trip.end_date >= since)
        .options(selectinload(Trip.potential_drivers).selectinload(PotentialDriver.user))
        .order_by(Trip.start_date.desc(), Trip.duty)
    )
    all_strikes = await session.scalars(
        select(Strike)
        .options(selectinload(Strike.user), selectinload(Strike.issued_by))
        .order_by(Strike.created_at.desc())
        .limit(50)
    )
    return render(
        request,
        "admin/index.html",
        user,
        users=all_users,
        trips=list(trips),
        strikes=list(all_strikes),
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
    try:
        new = await users.create(
            session,
            name=name,
            room_number=room_number,
            username=username,
            password=password,
            is_admin=is_admin,
        )
        await duties.fill_current(session, settings.today())
        await session.commit()
        flash(request, f"{new.display_name} hinzugefügt")
    except UserError as e:
        flash(request, str(e), "error")
    return redirect("/admin")


async def _get_user(session: AsyncSession, user_id: int) -> User:
    """The user, or a 404 page."""
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    return target


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
    target = await _get_user(session, user_id)
    if password and len(password) < auth.MIN_PASSWORD_LENGTH:
        flash(request, auth.PASSWORD_TOO_SHORT, "error")
        return redirect("/admin")
    if room_number.strip():
        try:
            target.room_number = users.check_room(room_number)
        except UserError as e:
            flash(request, str(e), "error")
            return redirect("/admin")
    target.name = name.strip() or target.name
    if target.id != user.id:
        target.is_admin = is_admin
    if password:
        target.password_hash = auth.hash_password(password)
    await session.commit()
    flash(request, f"{target.display_name} gespeichert")
    return redirect("/admin")


@router.post("/admin/users/{user_id}/active")
async def admin_toggle_active(request: Request, session: Session, user: AdminUser, user_id: int):
    """Pause or resume a resident."""
    target = await _get_user(session, user_id)
    await users.set_active(session, target, not target.active, settings.today())
    await session.commit()
    flash(request, f"{target.display_name} ist jetzt {'aktiv' if target.active else 'inaktiv'}")
    return redirect("/admin")


@router.post("/admin/users/{user_id}/delete")
async def admin_delete_user(request: Request, session: Session, user: AdminUser, user_id: int):
    """Delete a resident who moved out (not yourself)."""
    target = await _get_user(session, user_id)
    if target.id == user.id:
        flash(request, "Du kannst dich nicht selbst löschen", "error")
        return redirect("/admin")
    name = target.display_name
    await users.delete(session, target, settings.today())
    await session.commit()
    flash(request, f"{name} gelöscht")
    return redirect("/admin")


@router.post("/admin/strikes")
async def admin_issue_strike(
    request: Request,
    session: Session,
    user: AdminUser,
    user_id: Annotated[int, Form()],
    reason: Annotated[str, Form()],
):
    """Give a strike by hand."""
    target = await _get_user(session, user_id)
    await strikes.issue(
        session,
        user_id=target.id,
        reason=reason.strip() or "–",
        issued_by_id=user.id,
        today=settings.today(),
    )
    await session.commit()
    flash(request, f"Strike an {target.display_name} vergeben")
    return redirect("/admin")


@router.post("/admin/strikes/{strike_id}/revoke")
async def admin_revoke_strike(request: Request, session: Session, user: AdminUser, strike_id: int):
    """Take a strike back."""
    strike = await session.get(Strike, strike_id)
    if strike is None:
        raise HTTPException(404)
    await strikes.revoke(session, strike, settings.now())
    await session.commit()
    flash(request, "Strike zurückgenommen")
    return redirect("/admin")


@router.post("/admin/potential-drivers/{potential_driver_id}/excuse")
async def admin_excuse(
    request: Request, session: Session, user: AdminUser, potential_driver_id: int
):
    """Excuse a duty: no strike, no drive."""
    try:
        pd = await duties.get_potential_driver(session, potential_driver_id)
        await duties.excuse(session, pd, user, settings.now())
        await session.commit()
        flash(request, "Dienst entschuldigt")
    except DutyError as e:
        flash(request, str(e), "error")
    return redirect("/admin")


@router.post("/admin/potential-drivers/{potential_driver_id}/dispute")
async def admin_dispute(
    request: Request, session: Session, user: AdminUser, potential_driver_id: int
):
    """Marked as driven but not actually done: the driver gets a strike."""
    try:
        pd = await duties.get_potential_driver(session, potential_driver_id)
        await duties.dispute(session, pd, user, settings.now())
        await session.commit()
        name = pd.user.display_name if pd.user else "ehemalig"
        flash(request, f"Beanstandet, {name} bekommt einen Strike")
    except DutyError as e:
        flash(request, str(e), "error")
    return redirect(safe_next(request.query_params.get("next") or "/history"))


@router.post("/admin/tick")
async def admin_tick(request: Request, session: Session, user: AdminUser):
    """Run the daily job now."""
    await jobs.daily_tick(session, settings.now())
    flash(request, "Tagesjob ausgeführt")
    return redirect("/admin")
