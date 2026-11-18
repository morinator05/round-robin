"""The Telegram bot: posts each morning who is on duty in the floor's group chat.

It only sends (the Bot API's `sendMessage`) and reads nothing from Telegram. Off unless
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. The post goes out once a day right
after the daily job (`jobs.scheduled_tick`); `jobs.catch_up` tries again every 15 minutes
in case Telegram couldn't be reached then. `Announcement` rows say what was posted: the
day's post, and the week's trash duty, which comes with the first post of its week.
"""

import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.config import settings
from app.db import Lock, advisory_lock
from app.formatting import clock, month_name, trip_dates
from app.models import Announcement, Duty, Status, Trip, User
from app.services import dishwasher, duties
from app.services.errors import TelegramError

log = logging.getLogger(__name__)
# httpx logs every request's URL at INFO, and the bot token is part of the URL.
logging.getLogger("httpx").setLevel(logging.WARNING)

API = "https://api.telegram.org"
# `Announcement.kind`s: the morning post (its day), and the week's trash duty in one
# (the week's first day).
MORNING = "morning"
TRASH = "trash"
# How requests are sent; None is the network. Tests put a fake Telegram here.
transport: httpx.AsyncBaseTransport | None = None


async def send(text: str) -> int:
    """Post `text` in the floor's chat (in its topic, if set); returns the message's id."""
    if not settings.telegram_enabled:
        raise TelegramError("Telegram isn't set up (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID).")
    message = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }
    if settings.telegram_thread_id is not None:
        message["message_thread_id"] = settings.telegram_thread_id
    url = f"{API}/bot{settings.telegram_bot_token}/sendMessage"
    # httpx's errors are not passed on (`from None`): they can name the URL, and with it
    # the token.
    try:
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            response = await client.post(url, json=message)
        answer = response.json()
    except httpx.InvalidURL:
        raise TelegramError("TELEGRAM_BOT_TOKEN isn't a bot token.") from None
    except (httpx.HTTPError, ValueError):
        raise TelegramError("Telegram can't be reached right now.") from None
    if not isinstance(answer, dict) or not answer.get("ok"):
        why = answer.get("description") if isinstance(answer, dict) else None
        raise TelegramError(f"Telegram refused the message: {why or response.status_code}")
    try:
        return int(answer["result"]["message_id"])
    except (KeyError, TypeError, ValueError):
        raise TelegramError("Telegram sent an answer that makes no sense.") from None


async def send_test() -> None:
    """A test message, so the floor speaker can check the settings (admin page)."""
    name = f"Round Robin {settings.floor_name}".strip()
    await send(
        f"Test message from {name}: the bot can post here. Every day at "
        f"{settings.deadline_hour:02d}:05 it posts who is on duty."
    )


def names(people: list[User]) -> str:
    """The people as text: "A (E101)", "A (E101) and B (E102)", "A, B and C"."""
    shown = [p.display_name for p in people]
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"


def duty_line(intro: str, trip: Trip, today: date) -> str:
    """One trip's line: "<intro>: <who>. <what to do by when>.", or who did it already.

    Only those who still have to do it are named: not who was excused, and nobody once
    it's done. Done is credited to whose duty it was, also when the floor speaker marked it.
    """
    if (driver := trip.driver) is not None:
        return f"{intro}: done by {driver.user.display_name}." if driver.user else f"{intro}: done."
    people = [
        pd.user
        for pd in trip.potential_drivers
        if pd.status is Status.pending and pd.user is not None
    ]
    if not people:
        excused = any(pd.status is Status.excused for pd in trip.potential_drivers)
        return f"{intro}: {'excused' if excused else 'nobody available'}."
    due = duties.deadline(trip)
    when = "tomorrow" if due.date() == today + timedelta(days=1) else f"next {due:%A}"
    todo = "One of you cleans and marks it done" if len(people) > 1 else "Mark it done"
    penalty = " (penalty week)" if trip.is_penalty else ""
    return f"{intro}: {names(people)}{penalty}. {todo} by {clock(due)} {when}."


def morning_text(kitchen: Trip, trash: Trip | None, heroes: list[User], today: date) -> str:
    """The morning post: today's kitchen duty, the week's trash duty (`trash`, given in the
    week's first post), the month's dishwasher heroes (given on the 1st) and the link."""
    lines = [duty_line(f"Kitchen duty today, {trip_dates(kitchen)}", kitchen, today)]
    if trash is not None:
        lines.append(duty_line(f"Trash duty this week, {trip_dates(trash)}", trash, today))
    if heroes:
        last_month = month_name(dishwasher.previous_month(today))
        lines.append(
            f"Dishwasher heroes of {last_month}: {names(heroes)}. "
            f"No kitchen duty for them in {month_name(today)}."
        )
    if settings.app_url:
        lines.append(settings.app_url)
    return "\n".join(lines)


async def announce_morning(session: AsyncSession, now: datetime) -> bool:
    """Post today's duties (`morning_text`) once, after the day's people are chosen.

    Does nothing if Telegram is off, before the daily job has chosen today's kitchen duty
    (`duties.choose_at`), or if today's post is out already. Only today's is ever posted,
    so after the server was down nothing old is. Commits; True if it posted.
    """
    today = now.date()
    if not settings.telegram_enabled or now < duties.choose_at(Duty.kitchen, today):
        return False
    # One post a day, also when the scheduler's job and its retry run at the same time.
    await advisory_lock(session, Lock.telegram)
    try:
        posted = await _post_morning(session, now)
    except BaseException:
        await session.rollback()
        raise
    await session.commit()
    return posted


async def _post_morning(session: AsyncSession, now: datetime) -> bool:
    today = now.date()
    if await _posted(session, MORNING, today):
        return False
    kitchen = await duties.current_trip(session, Duty.kitchen, now)
    if kitchen is None or kitchen.start_date != today:
        return False  # the daily job hasn't run yet; a later try posts it
    # The week's trash duty, in its first post that goes out: on Monday normally, later if
    # the server or Telegram was down then.
    trash = await duties.current_trip(session, Duty.trash, now)
    if trash is not None and await _posted(session, TRASH, trash.start_date):
        trash = None
    heroes = await _heroes(session, today) if today.day == 1 else []
    message_id = await send(morning_text(kitchen, trash, heroes, today))
    session.add(Announcement(kind=MORNING, day=today, message_id=message_id, sent_at=now))
    if trash is not None:
        session.add(
            Announcement(kind=TRASH, day=trash.start_date, message_id=message_id, sent_at=now)
        )
    return True


async def _posted(session: AsyncSession, kind: str, day: date) -> bool:
    """Whether the `kind` post of `day` went out."""
    found = await session.scalar(
        select(Announcement.id).where(Announcement.kind == kind, Announcement.day == day)
    )
    return found is not None


async def _heroes(session: AsyncSession, today: date) -> list[User]:
    """This month's dishwasher heroes, in their order."""
    ids = await dishwasher.heroes(session, today)
    found = {u.id: u for u in await session.scalars(select(User).where(User.id.in_(ids)))}
    return [found[uid] for uid in ids if uid in found]


async def scheduled_announce() -> None:
    """`announce_morning` with its own session, for the scheduler.

    Failures are logged, not raised: the duties go on without Telegram.
    """
    try:
        async with db.SessionLocal() as session:
            if await announce_morning(session, settings.now()):
                log.info("Posted today's duties on Telegram")
    except TelegramError as e:
        log.warning("Telegram post failed: %s", e)
    except Exception:
        log.exception("Telegram post failed")
