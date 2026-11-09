"""Residents: creating, pausing, moving out, and away periods."""

import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    MAX_NAME_LENGTH,
    MAX_USERNAME_LENGTH,
    AwayPeriod,
    Duty,
    PotentialDriver,
    Status,
    Trip,
    User,
)
from app.services import carpool, duties, strikes
from app.services.auth import check_password, end_sessions, hash_password_off_loop
from app.services.errors import UserError

# Away periods can't end later than this many days from today.
MAX_AWAY_DAYS_AHEAD = 365
# Building letter, floor digit, room 01-99 on that floor: E103.
ROOM_PATTERN = re.compile(r"[A-Z][0-9](0[1-9]|[1-9][0-9])")


def check_name(name: str) -> str:
    """The name, trimmed.

    Raises:
        UserError: if it is empty or too long.
    """
    name = name.strip()
    if not name:
        raise UserError("The name is required")
    if len(name) > MAX_NAME_LENGTH:
        raise UserError(f"The name can have at most {MAX_NAME_LENGTH} characters")
    return name


def check_username(username: str) -> str:
    """The username, trimmed and in lower case (logins are case-insensitive).

    Raises:
        UserError: if it is empty or too long.
    """
    username = username.strip().lower()
    if not username:
        raise UserError("The username is required")
    if len(username) > MAX_USERNAME_LENGTH:
        raise UserError(f"The username can have at most {MAX_USERNAME_LENGTH} characters")
    return username


def check_room(room_number: str) -> str:
    """The room number, trimmed and upper case.

    Raises:
        UserError: if it isn't like E103 (letter, floor, room 01-99).
    """
    room_number = room_number.strip().upper()
    if not ROOM_PATTERN.fullmatch(room_number):
        raise UserError(f'Invalid room "{room_number}": letter, floor, room 01–99, e.g. E103')
    return room_number


async def list_users(session: AsyncSession) -> list[User]:
    """All users, sorted by room."""
    users = await session.scalars(select(User))
    return sorted(users, key=lambda u: carpool.room_key(u.room_number))


async def create(
    session: AsyncSession,
    *,
    name: str,
    room_number: str,
    username: str,
    password: str,
    is_admin: bool = False,
) -> User:
    """Add a resident. Their score starts at zero.

    Raises:
        UserError: if a field is empty, the room number or password is invalid, or the
            username is taken.
    """
    if not (name.strip() and room_number.strip() and username.strip() and password):
        raise UserError("All fields are required")
    name = check_name(name)
    username = check_username(username)
    room_number = check_room(room_number)
    check_password(password)
    if await session.scalar(select(User).where(User.username == username)):
        raise UserError(f'Username "{username}" is already taken')
    user = User(
        name=name,
        room_number=room_number,
        username=username,
        password_hash=await hash_password_off_loop(password),
        is_admin=is_admin,
        active=True,
    )
    session.add(user)
    await session.flush()
    return user


async def create_initial_admin(session: AsyncSession) -> User | None:
    """Create the floor speaker from the `ADMIN_*` settings, if set and there are no users yet.

    Only an empty database counts, so an admin deleted later doesn't come back on restart.

    Raises:
        UserError: if the settings are incomplete or invalid (e.g. the password is too short).
    """
    if not settings.admin_username:
        return None
    if await session.scalar(select(User.id).limit(1)) is not None:
        return None
    return await create(
        session,
        name=settings.admin_name,
        room_number=settings.admin_room,
        username=settings.admin_username,
        password=settings.admin_password,
        is_admin=True,
    )


async def update(
    session: AsyncSession,
    user: User,
    actor: User,
    *,
    name: str,
    room_number: str,
    password: str = "",
    is_admin: bool = False,
) -> None:
    """The floor speaker (`actor`) changes a resident's name, room, password or role.

    An empty name, room or password stays as it was, and nobody changes their own role. A
    new password logs the resident out everywhere.

    Raises:
        UserError: if a value is invalid; then nothing is changed.
    """
    if password:
        check_password(password)
    room_number = check_room(room_number) if room_number.strip() else user.room_number
    name = check_name(name) if name.strip() else user.name
    user.room_number, user.name = room_number, name
    if user.id != actor.id:
        user.is_admin = is_admin
    if password:
        user.password_hash = await hash_password_off_loop(password)
        end_sessions(user)
    await session.flush()


async def set_active(session: AsyncSession, user: User, active: bool, now: datetime) -> None:
    """Pause or resume a resident.

    A paused resident's open duties go to someone else, and their open penalty week waits
    until they are back.
    """
    user.active = active
    await session.flush()
    if active:
        await strikes.check_threshold(session, user.id, now.date())
        await duties.fill_current(session, now)
    else:
        await duties.reconcile(session, now.date())


async def delete(session: AsyncSession, user: User, now: datetime) -> None:
    """Moving out: their open duties go to someone else.

    Their history stays, without their name; whoever moves in next is a new user and starts
    from zero (score 0, no strikes).
    """
    today = now.date()
    # Open penalty weeks go with the person, so those weeks get a regular trash duty again.
    open_penalties = await session.scalars(
        select(Trip)
        .join(Trip.potential_drivers)
        .where(
            Trip.is_penalty.is_(True),
            Trip.end_date >= today,
            PotentialDriver.user_id == user.id,
            PotentialDriver.status == Status.pending,
        )
    )
    for trip in open_penalties.all():
        await session.delete(trip)
    await session.delete(user)
    await session.flush()
    await duties.reconcile(session, today)
    await duties.fill_current(session, now)


async def add_away(
    session: AsyncSession, user: User, start: date, end: date, now: datetime
) -> list[Duty]:
    """Mark a resident away from `start` to `end`: they aren't chosen during that time.

    A duty they already have stays theirs (the floor speaker can excuse it); an upcoming
    penalty week moves to their next free week.

    Returns:
        The duties whose current trip falls into the absence and stays theirs.

    Raises:
        UserError: if the period is reversed, already over or ends too far ahead.
    """
    today = now.date()
    if end < start:
        raise UserError("The end can't be before the start")
    if end < today:
        raise UserError("Away periods in the past don't change anything any more")
    if end > today + timedelta(days=MAX_AWAY_DAYS_AHEAD):
        raise UserError(
            f"Away periods can end at most {MAX_AWAY_DAYS_AHEAD} days from today; "
            "for longer, ask the floor speaker to pause you"
        )
    session.add(AwayPeriod(user_id=user.id, start_date=start, end_date=end))
    await session.flush()
    await duties.reconcile(session, today, keep_user_id=user.id)
    kept = []
    for duty in Duty:
        trip = await duties.current_trip(session, duty, now)
        if (
            trip is not None
            and trip.start_date <= end
            and start <= trip.end_date
            and any(
                pd.user_id == user.id and pd.status is Status.pending
                for pd in trip.potential_drivers
            )
        ):
            kept.append(duty)
    return kept


async def remove_away(session: AsyncSession, away: AwayPeriod) -> None:
    """Delete an away period. Duties already reassigned stay with their new people."""
    await session.delete(away)
    await session.flush()
