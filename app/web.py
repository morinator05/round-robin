"""All pages and form actions (server-rendered Jinja templates, htmx for small updates).

Routes only parse input, call a service and render or redirect; the rules live in
`app/services/`. Every form POST answers with a redirect (303), so reloading the page
doesn't submit the form again.

Only the login and logout (`public`) work without a session. Every route on `router`
requires a login, also new ones, so nothing personal is visible to the rest of the network.
"""

from datetime import date, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import deps
from app.config import settings
from app.deps import AdminUser, CurrentUser, OptionalUser, Session, require_user
from app.formatting import long_date, short_date
from app.models import (
    AwayPeriod,
    DishwasherClear,
    Duty,
    PotentialDriver,
    Status,
    Strike,
    Trip,
    User,
)
from app.services import auth, carpool, dishwasher, duties, jobs, strikes, users
from app.services.dishwasher import DishwasherError
from app.services.duties import DutyError
from app.services.login_throttle import account_throttle, login_throttle
from app.services.users import UserError

public = APIRouter()
router = APIRouter(dependencies=[Depends(require_user)])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def person_html(user: User | None, is_me: bool = False) -> Markup:
    """A person as HTML: the first name stands out, the room is a quieter tag after it.

    Both sit in one `.who` that doesn't wrap, so a name never ends up on another line than
    its room. `is_me` highlights the logged-in user's own entry, so they find themselves
    quickly.
    """
    if user is None:
        return Markup('<span class="muted">former resident</span>')
    return Markup(
        '<span class="{}"><span class="person-name">{}</span> <span class="room">{}</span></span>'
    ).format("who me" if is_me else "who", user.first_name, user.room_number)


@pass_context
def person(context, user: User | None) -> Markup:
    """`person_html` for templates: takes the logged-in user from the page's `user`."""
    viewer = context.get("user")
    is_me = user is not None and viewer is not None and user.id == viewer.id
    return person_html(user, is_me)


def help_link(topic: str) -> Markup:
    """A small "?" after a heading, linking to that topic on the help page."""
    return Markup(
        '<a class="help-link" href="/help#{}" title="How does this work?" '
        'aria-label="Help: {}">?</a>'
    ).format(topic, topic.replace("-", " "))


templates.env.globals["person"] = person
templates.env.globals["help_link"] = help_link
templates.env.globals["Duty"] = Duty
templates.env.globals["Status"] = Status
templates.env.globals["strike_threshold"] = settings.strike_threshold
templates.env.globals["dishwasher_cooldown"] = settings.dishwasher_cooldown_minutes
templates.env.globals["dishwasher_heroes"] = settings.dishwasher_heroes
templates.env.globals["floor_name"] = settings.floor_name
templates.env.globals["min_password_length"] = auth.MIN_PASSWORD_LENGTH
# Appended to the stylesheet URL, so browsers load it again after it changed instead of
# keeping an old copy from their cache.
templates.env.globals["css_version"] = int(
    (Path(__file__).parent / "static" / "style.css").stat().st_mtime
)
templates.env.filters["short_date"] = short_date
templates.env.filters["long_date"] = long_date
templates.env.filters["date_time"] = lambda d: (
    f"{long_date(d.astimezone(settings.tz))} {d.astimezone(settings.tz):%H:%M}" if d else ""
)
# Points with their sign: "+37", "−112" (a real minus sign), "0".
templates.env.filters["signed"] = lambda n: f"{n:+d}".replace("-", "\u2212") if n else "0"
templates.env.filters["weekday"] = lambda d: f"{d:%a}"
templates.env.filters["month_name"] = lambda d: f"{d:%B}"
templates.env.filters["clock"] = dishwasher.clock
templates.env.filters["local_date"] = lambda d: d.astimezone(settings.tz).date()


def flash(request: Request, message: str, kind: str = "ok") -> None:
    """Queue a message for the next rendered page. `kind` is "ok" or "error"."""
    # Assign a new list: the session only notices assignments, not in-place changes.
    request.session["flash"] = [*request.session.get("flash", []), [kind, message]]


def render(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    """Render a full page; adds the user, pending messages and today's date.

    htmx requests that only take a part of the page (the card after "Done", switching the
    plan) leave the messages queued: they would be dropped unseen. Boosted navigation shows
    the whole page, so it takes them.
    """
    partial = request.headers.get("HX-Request") and not request.headers.get("HX-Boosted")
    messages = [] if partial else request.session.pop("flash", [])
    return templates.TemplateResponse(
        request, name, {"user": user, "messages": messages, "today": settings.today(), **ctx}
    )


def start_session(request: Request, user: User) -> None:
    """Log `user` in, in this browser (for at most `deps.SESSION_MAX_SECONDS`)."""
    request.session["user_id"] = user.id
    request.session["token"] = user.session_token
    request.session["logged_in_at"] = int(deps.clock())


def redirect(url: str) -> RedirectResponse:
    """Redirect after a form POST; 303 makes the browser follow it with a GET."""
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None) -> str:
    """`url` if it's a path on this site, else "/". Stops redirects to other sites.

    Browsers read `\\` like `/` and skip tabs and line breaks, so `/\\evil.com` would
    leave the site too.
    """
    if (
        url
        and url.startswith("/")
        and not url.startswith("//")
        and "\\" not in url
        and url.isprintable()
    ):
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
        # Before the deadline hour the kitchen card still shows yesterday's duty.
        "day": (
            "today"
            if trip is None or trip.start_date == now.date()
            else "yesterday"
            if trip.start_date == now.date() - timedelta(days=1)
            else None
        ),
        "deadline": duties.deadline(trip) if trip else None,
        "can_mark_as_driver": (
            lambda pd: user is not None and duties.can_mark_as_driver(pd, user, now)
        ),
        "can_undo": lambda pd: user is not None and duties.can_undo(pd, user, now),
    }


# --- auth -------------------------------------------------------------------


@public.get("/login")
async def login_page(request: Request, user: OptionalUser, next: str = "/"):
    """Login form; `next` is where to go after logging in."""
    if user:
        return redirect(safe_next(next))
    return render(request, "login.html", None, next=next)


@public.post("/login")
async def login(
    request: Request,
    session: Session,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    """Log in. Too many failed attempts block the username from that IP for a while, and
    (with a higher limit, so others can't easily lock someone out) from everywhere."""
    client_ip = request.client.host if request.client else "unknown"
    # No real account has a longer username or password; don't keep or hash such input.
    if len(username) > users.MAX_USERNAME_LENGTH or len(password) > auth.MAX_PASSWORD_LENGTH:
        flash(request, "Wrong username or password", "error")
        return redirect(f"/login?next={safe_next(next)}")
    name = username.strip().lower()
    throttle_key, account_key = f"{name}@{client_ip}", f"user:{name}"
    if not (login_throttle.try_attempt(throttle_key) and account_throttle.try_attempt(account_key)):
        flash(request, auth.TOO_MANY_ATTEMPTS, "error")
        return redirect(f"/login?next={safe_next(next)}")

    user = await auth.authenticate(session, username, password)
    if user is None:
        flash(request, "Wrong username or password", "error")
        return redirect(f"/login?next={safe_next(next)}")
    login_throttle.reset(throttle_key)
    account_throttle.reset(account_key)
    start_session(request, user)
    return redirect(safe_next(next))


@public.post("/logout")
async def logout(request: Request, session: Session, user: OptionalUser):
    """Log out, on all devices. Only with a valid session: an outdated cookie ends nothing."""
    if user:
        auth.end_sessions(user)
        await session.commit()
    request.session.clear()
    return redirect("/")


# --- dashboard --------------------------------------------------------------


@router.get("/")
async def dashboard(
    request: Request, session: Session, user: CurrentUser, cleaner: str | None = None
):
    """Today's duties and the preview.

    While nobody has driven today's kitchen trip, the preview has one plan per potential
    driver; `cleaner` picks which one is shown (default: yourself if you're one of them,
    else the first).
    """
    now = settings.now()
    today = now.date()
    # Before the deadline hour that's still yesterday's (last week's) duty.
    kitchen = await duties.current_trip(session, Duty.kitchen, now)
    trash = await duties.current_trip(session, Duty.trash, now)
    if kitchen is None or trash is None:
        await jobs.daily_tick(session, now)
        kitchen = await duties.current_trip(session, Duty.kitchen, now)
        trash = await duties.current_trip(session, Duty.trash, now)

    def after(trip: Trip | None) -> date:
        """The preview starts with the first trip after the one on the card."""
        return trip.end_date + timedelta(days=1) if trip else today

    horizon = today + timedelta(days=settings.preview_days)
    kitchen_preview = await duties.preview(session, Duty.kitchen, after(kitchen), horizon, today)
    trash_preview = await duties.preview(session, Duty.trash, after(trash), horizon, today)
    people = await users_by_id(session)
    dishwasher_card = await dishwasher_ctx(session, user, now, people)

    choices = [uid for uid in kitchen_preview.plans if uid is not None]
    # Lenient: anything that isn't one of the choices (also "abc") shows the default.
    # isdigit() alone would let "²" through, which int() rejects (as very long numbers).
    valid = cleaner and cleaner.isascii() and cleaner.isdigit() and len(cleaner) <= 18
    chosen = int(cleaner) if valid else None
    if chosen not in kitchen_preview.plans:
        chosen = user.id if user.id in choices else next(iter(kitchen_preview.plans))
    kitchen_plan = kitchen_preview.plans[chosen]
    # The open trip's potential drivers who aren't up again within the plan (usually
    # whoever cleans): said below it, so nobody wonders where they went.
    shown = {uid for e in kitchen_plan for uid in e.potential_drivers}
    later = [people[uid] for uid in choices if uid not in shown and uid in people]
    kitchen_card = duty_card_ctx(kitchen, Duty.kitchen, user)
    trash_plan = next(iter(trash_preview.plans.values()))
    return render(
        request,
        "dashboard.html",
        user,
        you=await you_ctx(
            session, user, kitchen_card, trash, kitchen_preview, chosen, trash_plan, people
        ),
        kitchen=kitchen_card,
        trash=duty_card_ctx(trash, Duty.trash, user),
        kitchen_plan=kitchen_plan,
        kitchen_choices=[people[uid] for uid in choices if uid in people],
        kitchen_cleaner=chosen,
        kitchen_open_start=kitchen_preview.open_start,
        yesterday=today - timedelta(days=1),
        kitchen_later=later,
        trash_plan=trash_plan,
        preview_days=settings.preview_days,
        users_by_id=people,
        dishwasher=dishwasher_card,
    )


def _next_in(plan: list[duties.PreviewEntry], user_id: int) -> duties.PreviewEntry | None:
    """The user's first trip in a preview plan, if they are in it."""
    return next((e for e in plan if user_id in e.potential_drivers), None)


async def you_ctx(
    session: AsyncSession,
    user: User,
    kitchen_card: dict,
    trash: Trip | None,
    kitchen_preview: duties.Preview,
    chosen: int | None,
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

    kitchen_next = _next_in(kitchen_preview.plans[chosen], user.id)
    # Who comes up next depends on who cleans the open trip: name the other outcomes for
    # you if they'd move your next kitchen day.
    alternatives = []
    for uid, plan in kitchen_preview.plans.items():
        other = _next_in(plan, user.id)
        if uid not in (None, chosen) and (other and other.start) != (
            kitchen_next and kitchen_next.start
        ):
            alternatives.append(
                {"entry": other, "cleaner": people.get(uid), "is_me": uid == user.id}
            )
    history = await session.scalar(
        select(func.count(PotentialDriver.id)).where(PotentialDriver.user_id == user.id)
    )
    return {
        "inactive": not user.active,
        "kitchen_now": has(kitchen_card["trip"], Status.pending),
        "kitchen_done": has(kitchen_card["trip"], Status.drove),
        "kitchen_day": kitchen_card["day"],
        "trash_now": has(trash, Status.pending),
        "trash_done": has(trash, Status.drove),
        "kitchen_next": kitchen_next,
        "kitchen_alternatives": alternatives,
        "trash_next": _next_in(trash_plan, user.id),
        "strikes": len(await strikes.active_strikes(session, user.id)),
        "is_new": not history,
    }


async def dishwasher_ctx(
    session: AsyncSession, user: User, now: datetime, people: dict[int, User]
) -> dict:
    """Template variables for `partials/dishwasher_card.html` and the hero line above the
    kitchen preview.

    `state` is what the user's row shows: "can_mark" (the button), "done" (they emptied it
    last and the next one doesn't count yet), "not_needed" (someone else did) or "inactive".
    """
    today = now.date()
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
    standings = await dishwasher.standings(session, today)
    heroes = [people[uid] for uid in await dishwasher.heroes(session, today) if uid in people]
    return {
        "month": today,
        "last": last,
        "next_allowed": allowed if waiting else None,
        "state": state,
        "mine": next((s.count for s in standings if s.user_id == user.id), 0),
        "heroes": heroes,
        "is_hero": any(u.id == user.id for u in heroes),
        "other_heroes": [u for u in heroes if u.id != user.id],
        "heroes_from": dishwasher.previous_month(today),
    }


def is_card_request(request: Request) -> bool:
    """Whether a dashboard card posted this via htmx (`?card=1`). On success it gets the
    dashboard again and takes its own card from it (hx-select)."""
    return bool(request.headers.get("HX-Request") and request.query_params.get("card"))


@router.post("/dishwasher")
async def mark_dishwasher(request: Request, session: Session, user: CurrentUser):
    """You emptied the dishwasher; from the dashboard card."""
    card = is_card_request(request)
    try:
        await dishwasher.mark_cleared(session, user, settings.now())
        await session.commit()
    except DishwasherError as e:
        flash(request, str(e), "error")
        if card:
            # The card would drop the error message; reload the page so the flash shows.
            return Response(headers={"HX-Refresh": "true"})
        return redirect("/")
    if not card:
        flash(request, "Marked as emptied")
    return redirect("/")


@router.post("/potential-drivers/{potential_driver_id}/drove")
async def mark_as_driver(
    request: Request, session: Session, user: CurrentUser, potential_driver_id: int
):
    """Mark who drove (did the duty), from the dashboard card or the admin page."""
    # The dashboard card also takes the previews from the dashboard (hx-select-oob), so the
    # preview shows the now known outcome too.
    card = is_card_request(request)
    try:
        pd = await duties.get_potential_driver(session, potential_driver_id)
        await duties.mark_as_driver(session, pd, user, settings.now())
        await session.commit()
    except DutyError as e:
        flash(request, str(e), "error")
        if card:
            # The card would drop the error message; reload the page so the flash shows.
            return Response(headers={"HX-Refresh": "true"})
        return redirect(safe_next(request.query_params.get("next")))
    if card:
        return redirect("/")
    flash(request, "Marked as done")
    return redirect(safe_next(request.query_params.get("next")))


@router.post("/potential-drivers/{potential_driver_id}/undo")
async def undo_driver(
    request: Request, session: Session, user: CurrentUser, potential_driver_id: int
):
    """Take back a mis-tapped "Done", from the dashboard card or the admin page."""
    card = is_card_request(request)
    try:
        pd = await duties.get_potential_driver(session, potential_driver_id)
        await duties.undo_driver(session, pd, user, settings.now())
        await session.commit()
    except DutyError as e:
        flash(request, str(e), "error")
        if card:
            return Response(headers={"HX-Refresh": "true"})
        return redirect(safe_next(request.query_params.get("next")))
    if card:
        return redirect("/")
    flash(request, "Undone: the duty is open again")
    return redirect(safe_next(request.query_params.get("next")))


# --- personal page ----------------------------------------------------------


@router.get("/me")
async def me(request: Request, session: Session, user: CurrentUser):
    """Own page: away periods, password, strikes, dishwasher and recent duties."""
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
    today = settings.today()
    standings = await dishwasher.standings(session, today)
    return render(
        request,
        "me.html",
        user,
        aways=list(aways),
        strikes=list(my_strikes),
        history=list(history),
        dishwasher_mine=next((s.count for s in standings if s.user_id == user.id), 0),
        is_hero=user.id in await dishwasher.heroes(session, today),
        heroes_from=dishwasher.previous_month(today),
    )


@router.post("/me/away")
async def add_away(
    request: Request,
    session: Session,
    user: CurrentUser,
    start_date: Annotated[date, Form()],
    end_date: Annotated[date, Form()],
):
    """Add an away period for yourself. Says so if a duty you already have falls into it."""
    user_id, now = user.id, settings.now()
    try:
        await users.add_away(session, user, start_date, end_date, now.date())
        await session.commit()
    except UserError as e:
        flash(request, str(e), "error")
        return redirect("/me")
    flash(request, "Away period added")
    for duty in Duty:
        trip = await duties.current_trip(session, duty, now)
        if (
            trip is not None
            and trip.start_date <= end_date
            and start_date <= trip.end_date
            and any(
                pd.user_id == user_id and pd.status is Status.pending
                for pd in trip.potential_drivers
            )
        ):
            flash(
                request,
                f"Your current {duty.label.lower()} stays with you; if you can't do it, "
                "ask the floor speaker to excuse it",
            )
    return redirect("/me")


@router.post("/me/away/{away_id}/delete")
async def delete_away(request: Request, session: Session, user: CurrentUser, away_id: int):
    """Delete an away period (your own, or anyone's as floor speaker)."""
    away = await session.get(AwayPeriod, away_id)
    if away is None or (away.user_id != user.id and not user.is_admin):
        raise HTTPException(404)
    await users.remove_away(session, away)
    await session.commit()
    flash(request, "Away period deleted")
    # The admin page posts with `next=/admin`.
    return redirect(safe_next(request.query_params.get("next") or "/me"))


@router.post("/me/password")
async def change_password(
    request: Request,
    session: Session,
    user: CurrentUser,
    old_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
):
    """Change your own password. Wrong old passwords are limited like logins."""
    throttle_key = f"password-change:{user.id}"
    if not login_throttle.try_attempt(throttle_key):
        flash(request, auth.TOO_MANY_ATTEMPTS, "error")
    elif len(old_password) > auth.MAX_PASSWORD_LENGTH or not await auth.verify_password_off_loop(
        old_password, user.password_hash
    ):
        flash(request, "Old password is wrong", "error")
    else:
        # The old password was right, so the attempt doesn't count, even if the new one
        # is too short.
        login_throttle.reset(throttle_key)
        if error := auth.check_password_length(new_password):
            flash(request, error, "error")
        else:
            # Logs out the other devices; this browser stays logged in.
            user.password_hash = await auth.hash_password_off_loop(new_password)
            auth.end_sessions(user)
            await session.commit()
            start_session(request, user)
            flash(request, "Password changed")
    return redirect("/me")


# --- history ----------------------------------------------------------------

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


# --- stats ------------------------------------------------------------------


@router.get("/stats")
async def stats(request: Request, session: Session, user: CurrentUser):
    """Per resident: duties driven and missed, score and strikes."""
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
    counts_by_user = await strikes.active_counts(session)
    active = {u.id: counts_by_user.get(u.id, 0) for u in all_users}
    # Whole points per duty instead of fractions; ties go by who is chosen next.
    ids = [u.id for u in all_users]
    rooms = {u.id: u.room_number for u in all_users}
    points = {}
    for d, b in balances.items():
        queue = carpool.choose_potential_drivers(b, ids, len(ids), rooms)
        scores = {uid: b[uid].score if uid in b else Fraction(0) for uid in ids}
        points[d] = carpool.display_points(scores, queue)
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
    )


# --- admin ------------------------------------------------------------------


@router.get("/help")
async def help_page(request: Request, user: CurrentUser):
    """How the duties work, for new residents."""
    return render(
        request,
        "help.html",
        user,
        kitchen_people=settings.kitchen_people_per_day,
        trash_people=settings.trash_people_per_week,
        deadline_hour=settings.deadline_hour,
        preview_days=settings.preview_days,
    )


@router.get("/admin")
async def admin(request: Request, session: Session, user: AdminUser):
    """admin page: residents, strikes and recent duties."""
    now = settings.now()
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
    # Current and upcoming absences of everyone, so unusual ones are noticed.
    aways = await session.scalars(
        select(AwayPeriod)
        .where(AwayPeriod.end_date >= settings.today())
        .options(selectinload(AwayPeriod.user))
        .order_by(AwayPeriod.start_date, AwayPeriod.id)
    )
    return render(
        request,
        "admin/index.html",
        user,
        users=all_users,
        trips=list(trips),
        strikes=list(all_strikes),
        aways=list(aways),
        dishwasher_clears=await dishwasher.clears_in_month(session, settings.today()),
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
    try:
        new = await users.create(
            session,
            name=name,
            room_number=room_number,
            username=username,
            password=password,
            is_admin=is_admin,
        )
        await duties.fill_current(session, settings.now())
        await session.commit()
        flash(request, f"{new.display_name} added")
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
    if password and (error := auth.check_password_length(password)):
        flash(request, error, "error")
        return redirect("/admin")
    try:
        if room_number.strip():
            target.room_number = users.check_room(room_number)
        if name.strip():
            target.name = users.check_name(name)
    except UserError as e:
        await session.rollback()
        flash(request, str(e), "error")
        return redirect("/admin")
    if target.id != user.id:
        target.is_admin = is_admin
    if password:
        # Whoever knew the old password (or has a session) is logged out.
        target.password_hash = await auth.hash_password_off_loop(password)
        auth.end_sessions(target)
    await session.commit()
    if target.id == user.id:
        start_session(request, user)  # your own new password: stay logged in here
    flash(request, f"{target.display_name} saved")
    return redirect("/admin")


@router.post("/admin/users/{user_id}/active")
async def admin_toggle_active(request: Request, session: Session, user: AdminUser, user_id: int):
    """Pause or resume a resident."""
    target = await _get_user(session, user_id)
    await users.set_active(session, target, not target.active, settings.now())
    await session.commit()
    flash(request, f"{target.display_name} is now {'active' if target.active else 'inactive'}")
    return redirect("/admin")


@router.post("/admin/users/{user_id}/delete")
async def admin_delete_user(request: Request, session: Session, user: AdminUser, user_id: int):
    """Delete a resident who moved out (not yourself)."""
    target = await _get_user(session, user_id)
    if target.id == user.id:
        flash(request, "You can't delete yourself", "error")
        return redirect("/admin")
    name = target.display_name
    await users.delete(session, target, settings.now())
    await session.commit()
    flash(request, f"{name} deleted")
    return redirect("/admin")


@router.post("/admin/strikes")
async def admin_issue_strike(
    request: Request,
    session: Session,
    user: AdminUser,
    user_id: Annotated[str, Form()],
    reason: Annotated[str, Form()],
):
    """Give a strike by hand."""
    if not user_id.isdecimal():
        flash(request, "Choose who gets the strike", "error")
        return redirect("/admin")
    target = await _get_user(session, int(user_id))
    await strikes.issue(
        session,
        user_id=target.id,
        reason=reason.strip() or "–",
        issued_by_id=user.id,
        today=settings.today(),
    )
    await session.commit()
    flash(request, f"Strike given to {target.display_name}")
    return redirect("/admin")


@router.post("/admin/strikes/{strike_id}/revoke")
async def admin_revoke_strike(request: Request, session: Session, user: AdminUser, strike_id: int):
    """Take a strike back."""
    strike = await session.get(Strike, strike_id)
    if strike is None:
        raise HTTPException(404)
    await strikes.revoke(session, strike, settings.now())
    await session.commit()
    flash(request, "Strike revoked")
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
        flash(request, "Duty excused")
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
        name = pd.user.display_name if pd.user else "the former resident"
        flash(request, f"Disputed, {name} gets a strike")
    except DutyError as e:
        flash(request, str(e), "error")
    return redirect(safe_next(request.query_params.get("next") or "/history"))


@router.post("/admin/dishwasher/{clear_id}/delete")
async def admin_delete_dishwasher(
    request: Request, session: Session, user: AdminUser, clear_id: int
):
    """Remove a wrongly marked emptying of the dishwasher (current month only)."""
    clear = await session.get(DishwasherClear, clear_id)
    if clear is None:
        raise HTTPException(404)
    try:
        await dishwasher.delete_clear(session, clear, user, settings.now())
        await session.commit()
        flash(request, "Dishwasher entry deleted")
    except DishwasherError as e:
        flash(request, str(e), "error")
    return redirect("/admin")


@router.post("/admin/tick")
async def admin_tick(request: Request, session: Session, user: AdminUser):
    """Run the daily job now."""
    await jobs.daily_tick(session, settings.now())
    flash(request, "Daily job done")
    return redirect("/admin")
