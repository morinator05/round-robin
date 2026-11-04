"""Passwords (Argon2id hashes) and checking a login."""

from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

MIN_PASSWORD_LENGTH = 8
PASSWORD_TOO_SHORT = f"The password must have at least {MIN_PASSWORD_LENGTH} characters"

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


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    """The user with these credentials, or None. Usernames are case-insensitive."""
    user = await session.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
