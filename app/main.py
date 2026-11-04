"""The FastAPI app: middleware, static files, routes and the daily background job."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.db import SessionLocal
from app.deps import LoginRequired
from app.services import users
from app.services.jobs import scheduled_tick
from app.web import router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Creates the first floor speaker if configured, then runs the daily job once on startup
    and on a schedule while the app is up."""
    async with SessionLocal() as session:
        # Invalid ADMIN_* settings stop the startup, like a missing SECRET_KEY does.
        admin = await users.create_initial_admin(session)
        if admin:
            await session.commit()
            log.info("Created floor speaker @%s", admin.username)
    scheduler = None
    if settings.run_scheduler:
        scheduler = AsyncIOScheduler(timezone=settings.tz)
        # Right after the deadline hour: marks what was missed, then chooses the new day's
        # (and on Monday the new week's) potential drivers.
        trigger = CronTrigger(hour=settings.deadline_hour, minute=5, timezone=settings.tz)
        scheduler.add_job(scheduled_tick, trigger)
        scheduler.start()
        await scheduled_tick()
    yield
    if scheduler:
        scheduler.shutdown()


app = FastAPI(title=f"Round Robin {settings.floor_name}", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="a4_session",
    same_site="lax",
    max_age=60 * 60 * 24 * 90,  # stay logged in for 90 days
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(router)


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    """Send people who aren't logged in to the login page, and back here afterwards."""
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
