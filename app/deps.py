"""Request dependencies for the routes: the database session and the logged-in user.

A route parameter `user: AdminUser` loads the logged-in user and checks that they are the
floor speaker before the route body runs.
"""

import hmac
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User

Session = Annotated[AsyncSession, Depends(get_session)]

# A login lasts at most this long, however often it's used (the cookie itself is renewed
# with every response). Then the password is needed again.
SESSION_MAX_SECONDS = 90 * 24 * 60 * 60
# Injectable for tests.
clock = time.time


class LoginRequired(Exception):
    """Nobody is logged in; `main.py` turns this into a redirect to the login page."""


async def optional_user(request: Request, session: Session) -> User | None:
    """The logged-in user, or None.

    The session must name an existing user, carry their current `session_token` and be
    younger than `SESSION_MAX_SECONDS`; otherwise (user deleted, logged out elsewhere,
    password changed, database reset, too old) it is cleared.
    """
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    token = request.session.get("token")
    if (
        user is None
        or not isinstance(token, str)
        or not hmac.compare_digest(token, user.session_token)
    ):
        request.session.clear()
        return None
    logged_in_at = request.session.get("logged_in_at")
    if not isinstance(logged_in_at, int | float):
        # Logged in before the lifetime existed: it starts now, so nobody is logged out.
        request.session["logged_in_at"] = int(clock())
    elif clock() - logged_in_at > SESSION_MAX_SECONDS:
        request.session.clear()
        return None
    return user


async def require_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    """The logged-in user.

    Raises:
        LoginRequired: if nobody is logged in.
    """
    if user is None:
        raise LoginRequired
    return user


async def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    """The logged-in user, who must be the floor speaker (admin).

    Raises:
        HTTPException: 403 for everybody else.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Floor speaker only")
    return user


# Use these as route parameter types.
OptionalUser = Annotated[User | None, Depends(optional_user)]
CurrentUser = Annotated[User, Depends(require_user)]
AdminUser = Annotated[User, Depends(require_admin)]
