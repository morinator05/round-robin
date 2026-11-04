"""Residents: creating, pausing, moving out, and away periods."""

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AwayPeriod, PotentialDriver, Status, Trip, User
from app.services import carpool, duties
from app.services.auth import MIN_PASSWORD_LENGTH, PASSWORD_TOO_SHORT, hash_password


class UserError(Exception):
    """Invalid input; the message is German and shown to the user as is."""


# Building letter, floor digit, room 01-99 on that floor: A403.
ROOM_PATTERN = re.compile(r"[A-Z][0-9](0[1-9]|[1-9][0-9])")


def check_room(room_number: str) -> str:
    """The room number, trimmed and upper case.

    Raises:
        UserError: if it isn't like A403 (letter, floor, room 01-99).
    """
    room_number = room_number.strip().upper()
    if not ROOM_PATTERN.fullmatch(room_number):
        raise UserError(f'Invalid room "{room_number}": letter, floor, room 01–99, e.g. A403')
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
    username = username.strip().lower()
    if not (name.strip() and room_number.strip() and username and password):
        raise UserError("All fields are required")
    room_number = check_room(room_number)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(PASSWORD_TOO_SHORT)
    if await session.scalar(select(User).where(User.username == username)):
        raise UserError(f'Username "{username}" is already taken')
    user = User(
        name=name.strip(),
        room_number=room_number,
        username=username,
        password_hash=hash_password(password),
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


async def set_active(session: AsyncSession, user: User, active: bool, today: date) -> None:
    """Pause or resume a resident. A paused resident's open duties go to someone else."""
    user.active = active
    await session.flush()
    if active:
        await duties.fill_current(session, today)
    else:
        await duties.reconcile(session, today)


async def delete(session: AsyncSession, user: User, today: date) -> None:
    """Moving out. The person's history stays anonymised; whoever moves in next is a new
    user and starts from zero (score 0, no strikes).

    Their open duties go to someone else.
    """
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
    session.expire_all()
    await duties.reconcile(session, today)
    await duties.fill_current(session, today)


async def add_away(session: AsyncSession, user: User, start: date, end: date, today: date) -> None:
    """Mark a resident away from `start` to `end`; open duties in that time are reassigned.

    Raises:
        UserError: if the period is reversed or already over.
    """
    if end < start:
        raise UserError("The end can't be before the start")
    if end < today:
        raise UserError("Away periods in the past don't change anything any more")
    session.add(AwayPeriod(user_id=user.id, start_date=start, end_date=end))
    await session.flush()
    session.expire_all()
    await duties.reconcile(session, today)


async def remove_away(session: AsyncSession, away: AwayPeriod) -> None:
    """Delete an away period. Duties already reassigned stay with their new people."""
    await session.delete(away)
    await session.flush()
