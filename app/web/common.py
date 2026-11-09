"""What all pages share: the routers, the templates, messages, redirects and `act`."""

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, formatting
from app.config import settings
from app.deps import require_user
from app.models import MAX_ID, Duty, Status, User
from app.services import auth, strikes, users
from app.services.errors import ServiceError

APP_DIR = Path(__file__).parent.parent
# Only the login and logout work without a session. Every route on `router` requires a
# login, also new ones, so nothing personal is visible to the rest of the network.
public = APIRouter()
router = APIRouter(dependencies=[Depends(require_user)])
templates = Jinja2Templates(directory=APP_DIR / "templates")


def person_html(user: User | None, is_me: bool = False) -> Markup:
    """A person as HTML: the first name stands out, the room is a quieter tag after it.

    Both sit in one `.who` that doesn't wrap, so a name never ends up on another line than
    its room. `is_me` highlights the logged-in user's own entry.
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


templates.env.globals.update(
    person=person,
    help_link=help_link,
    near_penalty=strikes.near_penalty,
    Duty=Duty,
    Status=Status,
    strike_threshold=settings.strike_threshold,
    # The deadline of a duty, and when the next one is chosen (by the daily job, 5 minutes on).
    deadline_time=f"{settings.deadline_hour:02d}:00",
    assign_time=f"{settings.deadline_hour:02d}:05",
    dishwasher_cooldown=settings.dishwasher_cooldown_minutes,
    dishwasher_heroes=settings.dishwasher_heroes,
    floor_name=settings.floor_name,
    min_password_length=auth.MIN_PASSWORD_LENGTH,
    # For the `pattern` of room inputs, which can't ignore case.
    room_pattern=users.ROOM_PATTERN.pattern.replace("[A-Z]", "[A-Za-z]", 1),
    # Appended to the stylesheet URL, so browsers load it again after it changed instead of
    # keeping an old copy from their cache.
    css_version=int((APP_DIR / "static" / "style.css").stat().st_mtime),
)
templates.env.filters.update(
    {
        name: getattr(formatting, name)
        for name in (
            "short_date",
            "long_date",
            "date_time",
            "signed",
            "weekday",
            "month_name",
            "clock",
            "local_date",
            "times",
            "trip_dates",
        )
    }
)


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


def back(request: Request, default: str = "/") -> str:
    """Where a form goes back to: its `?next=` (on this site), else `default`."""
    return safe_next(request.query_params.get("next") or default)


def is_card_request(request: Request) -> bool:
    """Whether a dashboard card posted this via htmx (`?card=1`).

    On success it gets the dashboard again and takes its own card from it (hx-select).
    """
    return bool(request.headers.get("HX-Request") and request.query_params.get("card"))


async def act(
    request: Request,
    session: AsyncSession,
    action: Callable[[], Awaitable[str | None]],
    to: str = "/",
) -> Response:
    """Run `action` (service calls returning the message to show), commit, go to `to`.

    If a rule says no (`ServiceError`), nothing is saved and its message is shown instead.
    A dashboard card shows the outcome itself, so it gets no message on success, and on an
    error the whole page reloads: the card would drop the message.
    """
    card = is_card_request(request)
    try:
        message = await action()
        await session.commit()
    except ServiceError as e:
        await session.rollback()
        flash(request, str(e), "error")
        return Response(headers={"HX-Refresh": "true"}) if card else redirect(to)
    if message and not card:
        flash(request, message)
    return redirect("/" if card else to)


def parse_id(value: str) -> int | None:
    """`value` as a row id, or None if it can't be one (e.g. "" from an unchosen select)."""
    if value.isascii() and value.isdigit() and 0 < int(value) <= MAX_ID:
        return int(value)
    return None


async def get_row(session: AsyncSession, model: type, id: int):
    """The row of `model` with `id`, or None (also for ids no row can have)."""
    return await session.get(model, id) if 0 < id <= MAX_ID else None


async def get_or_404(session: AsyncSession, model: type, id: int):
    """The row of `model` with `id`, or a 404 page."""
    if (row := await get_row(session, model, id)) is None:
        raise HTTPException(404)
    return row


async def users_by_id(session: AsyncSession) -> dict[int, User]:
    """All users by id, to show names."""
    return {u.id: u for u in await session.scalars(select(User))}
