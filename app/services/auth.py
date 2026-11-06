"""Passwords (Argon2id hashes) and checking a login."""

from anyio import to_thread
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

MIN_PASSWORD_LENGTH = 8
PASSWORD_TOO_SHORT = f"The password must have at least {MIN_PASSWORD_LENGTH} characters"
TOO_MANY_ATTEMPTS = "Too many failed attempts, please try again in 15 minutes"

_hasher = PasswordHash.recommended()
# Checked when the username doesn't exist, so a wrong username takes as long as a wrong
# password and response times don't reveal which usernames exist.
_DUMMY_HASH = _hasher.hash("no-such-user")


def hash_password(password: str) -> str:
    """Hash for storing; includes a random salt, so equal passwords get different hashes."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True if `password` matches the stored hash."""
    return _hasher.verify(password, password_hash)


async def verify_password_off_loop(password: str, password_hash: str) -> bool:
    """`verify_password` in a worker thread. A check takes ~75 ms of CPU; on the event loop
    a few guesses per second would stall every other request."""
    return await to_thread.run_sync(verify_password, password, password_hash)


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    """The user with these credentials, or None. Usernames are case-insensitive."""
    user = await session.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        await verify_password_off_loop(password, _DUMMY_HASH)
        return None
    if not await verify_password_off_loop(password, user.password_hash):
        return None
    return user
