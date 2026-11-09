"""Your own page: absences, password, strikes, the dishwasher and your recent duties."""

from datetime import date
from typing import Annotated

from fastapi import Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.deps import CurrentUser, Session
from app.models import AwayPeriod, PotentialDriver, Strike, Trip
from app.services import auth, dishwasher, users
from app.services.errors import UserError
from app.web.common import act, flash, redirect, render, router, start_session


@router.get("/me")
async def me(request: Request, session: Session, user: CurrentUser):
    """Own page: away periods, password, strikes, dishwasher and recent duties."""
    aways = await session.scalars(
        select(AwayPeriod)
        .where(AwayPeriod.user_id == user.id, AwayPeriod.end_date >= settings.today())
        .order_by(AwayPeriod.start_date)
    )
    my_strikes = await session.scalars(
        select(Strike).where(Strike.user_id == user.id).order_by(Strike.created_at.desc())
    )
    history = await session.scalars(
        select(PotentialDriver)
        .join(PotentialDriver.trip)
        .where(PotentialDriver.user_id == user.id)
        .options(selectinload(PotentialDriver.trip))
        .order_by(Trip.start_date.desc())
        .limit(30)
    )
    summary = await dishwasher.summary_for(session, user, settings.today())
    return render(
        request,
        "me.html",
        user,
        aways=list(aways),
        strikes=list(my_strikes),
        history=list(history),
        dishwasher_mine=summary.mine,
        is_hero=summary.is_hero(user.id),
        heroes_from=summary.heroes_from,
    )


def stays_with(duties) -> list[str]:
    """The messages for duties that stay with someone despite their new absence."""
    return [
        f"Your current {duty.label.lower()} stays with you; if you can't do it, "
        "ask the floor speaker to excuse it"
        for duty in duties
    ]


@router.post("/me/away")
async def add_away(
    request: Request,
    session: Session,
    user: CurrentUser,
    start_date: Annotated[date, Form()],
    end_date: Annotated[date, Form()],
):
    """Add an away period for yourself. Says so if a duty you already have falls into it."""
    try:
        kept = await users.add_away(session, user, start_date, end_date, settings.now())
        await session.commit()
    except UserError as e:
        flash(request, str(e), "error")
        return redirect("/me")
    for message in ["Away period added", *stays_with(kept)]:
        flash(request, message)
    return redirect("/me")


@router.post("/me/away/{away_id}/delete")
async def delete_away(request: Request, session: Session, user: CurrentUser, away_id: int):
    """Delete one of your away periods."""
    away = await session.get(AwayPeriod, away_id)
    if away is None or away.user_id != user.id:
        raise HTTPException(404)

    async def delete():
        await users.remove_away(session, away)
        return "Away period deleted"

    return await act(request, session, delete, "/me")


@router.post("/me/password")
async def change_password(
    request: Request,
    session: Session,
    user: CurrentUser,
    old_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
):
    """Change your own password; this browser stays logged in, the others don't."""

    async def change():
        await auth.change_password(user, old_password, new_password)
        start_session(request, user)
        return "Password changed"

    return await act(request, session, change, "/me")
