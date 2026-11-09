"""Logging in and out."""

from typing import Annotated

from fastapi import Form, Request

from app.deps import OptionalUser, Session
from app.services import auth
from app.services.errors import UserError
from app.web.common import flash, public, redirect, render, safe_next, start_session


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
    """Log in (limited, see `auth.login`)."""
    client_ip = request.client.host if request.client else "unknown"
    try:
        user = await auth.login(session, username, password, client_ip)
    except UserError as e:
        flash(request, str(e), "error")
        return redirect(f"/login?next={safe_next(next)}")
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
