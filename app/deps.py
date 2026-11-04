"""Request dependencies for the routes: database session and the logged-in user.

FastAPI resolves these per request, similar to constructor injection. A route parameter
`user: AdminUser` loads the logged-in user and also checks that they are the floor speaker,
before the route body runs.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User

Session = Annotated[AsyncSession, Depends(get_session)]


class LoginRequired(Exception):
    """Nobody is logged in; `main.py` turns this into a redirect to the login page."""


async def optional_user(request: Request, session: Session) -> User | None:
    """The logged-in user, or None. A session of a since deleted user is cleared."""
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    if user is None:
        request.session.clear()
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
