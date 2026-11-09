"""Passwords (Argon2id hashes), logging in and changing your password."""

from anyio import to_thread
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MAX_USERNAME_LENGTH, User, new_session_token
from app.services import login_throttle
from app.services.errors import UserError

MIN_PASSWORD_LENGTH = 8
# Argon2 would hash megabytes too; longer input is refused before hashing.
MAX_PASSWORD_LENGTH = 1024
WRONG_LOGIN = "Wrong username or password"
TOO_MANY_ATTEMPTS = "Too many failed attempts, please try again in 15 minutes"

_hasher = PasswordHash.recommended()
# Checked when the username doesn't exist, so a wrong username takes as long as a wrong
# password and response times don't reveal which usernames exist.
_DUMMY_HASH = _hasher.hash("no-such-user")


def hash_password(password: str) -> str:
    """Hash for storing; includes a random salt, so equal passwords get different hashes."""
    return _hasher.hash(password)


async def hash_password_off_loop(password: str) -> str:
    """`hash_password` in a worker thread, like `verify_password_off_loop`."""
    return await to_thread.run_sync(hash_password, password)


def check_password(password: str) -> None:
    """Raises `UserError` if `password` is too short or too long for a new password."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"The password must have at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise UserError(f"The password can have at most {MAX_PASSWORD_LENGTH} characters")


def verify_password(password: str, password_hash: str) -> bool:
    """True if `password` matches the stored hash."""
    return _hasher.verify(password, password_hash)


async def verify_password_off_loop(password: str, password_hash: str) -> bool:
    """`verify_password` in a worker thread.

    A check takes ~75 ms of CPU; on the event loop a few guesses per second would stall
    every other request.
    """
    return await to_thread.run_sync(verify_password, password, password_hash)


def end_sessions(user: User) -> None:
    """Log `user` out on every device: their existing session cookies stop working."""
    user.session_token = new_session_token()


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    """The user with these credentials, or None. Usernames are case-insensitive."""
    user = await session.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        await verify_password_off_loop(password, _DUMMY_HASH)
        return None
    if not await verify_password_off_loop(password, user.password_hash):
        return None
    return user


async def login(session: AsyncSession, username: str, password: str, ip: str) -> User:
    """The user with these credentials, logging in from `ip`.

    Too many failed attempts block the username from that IP for a while, and (with a
    higher limit, so others can't easily lock someone out) from everywhere.

    Raises:
        UserError: if the credentials are wrong or there were too many attempts.
    """
    # No real account has a longer username or password; don't keep or hash such input.
    if len(username) > MAX_USERNAME_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        raise UserError(WRONG_LOGIN)
    name = username.strip().lower()
    ip_key, account_key = f"{name}@{ip}", f"user:{name}"
    if not (
        login_throttle.ip_throttle.try_attempt(ip_key)
        and login_throttle.account_throttle.try_attempt(account_key)
    ):
        raise UserError(TOO_MANY_ATTEMPTS)
    user = await authenticate(session, username, password)
    if user is None:
        raise UserError(WRONG_LOGIN)
    login_throttle.ip_throttle.reset(ip_key)
    login_throttle.account_throttle.reset(account_key)
    return user


async def change_password(user: User, old: str, new: str) -> None:
    """Change `user`'s own password and log them out on their other devices.

    Wrong old passwords are limited like logins, so someone with a session (an unlocked
    phone) can't find the password out by trying.

    Raises:
        UserError: if the old password is wrong, there were too many attempts, or the new
            one is too short or too long.
    """
    key = f"password-change:{user.id}"
    if not login_throttle.ip_throttle.try_attempt(key):
        raise UserError(TOO_MANY_ATTEMPTS)
    if len(old) > MAX_PASSWORD_LENGTH or not await verify_password_off_loop(
        old, user.password_hash
    ):
        raise UserError("Old password is wrong")
    # The old password was right, so the attempt doesn't count, even if the new one isn't.
    login_throttle.ip_throttle.reset(key)
    check_password(new)
    user.password_hash = await hash_password_off_loop(new)
    end_sessions(user)
