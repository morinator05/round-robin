"""The FastAPI app: middleware, static files, routes and the daily background job."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.db import SessionLocal
from app.deps import LoginRequired
from app.services import users
from app.services.jobs import scheduled_tick
from app.web import public, router

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
        # If the job couldn't run on time (e.g. a suspended host), run it once as soon as
        # possible instead of skipping it.
        scheduler.add_job(scheduled_tick, trigger, misfire_grace_time=None, coalesce=True)
        scheduler.start()
        try:
            await scheduled_tick()
        except Exception:
            # Don't keep the app from starting; the dashboard runs it again when needed.
            log.exception("Daily job on startup failed")
    yield
    if scheduler:
        scheduler.shutdown()


# No /docs, /redoc or /openapi.json: they would list every route to anyone on the network.
app = FastAPI(
    title=f"Round Robin {settings.floor_name}".strip(),
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="a4_session",
    same_site="lax",
    https_only=settings.secure_cookies,
    max_age=60 * 60 * 24 * 90,  # stay logged in for 90 days
)


@app.middleware("http")
async def refuse_cross_site_posts(request: Request, call_next):
    """CSRF protection: only this site's own pages may send forms here.

    SameSite=Lax alone isn't enough: to the browser, other ports on the same host and
    sibling subdomains are the "same site". Browsers say where a request comes from in
    `Sec-Fetch-Site`, older ones only in `Origin`; requests without either (scripts, very
    old browsers) carry no session cookie from another site anyway.
    """
    if request.method == "POST":
        fetch_site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        if fetch_site is not None and fetch_site not in ("same-origin", "none"):
            return PlainTextResponse("Cross-site request refused", status_code=403)
        if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-site request refused", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def no_caching_of_pages(request: Request, call_next):
    """Pages hold personal data: the browser must not keep them, e.g. for Back after
    logging out on a shared computer. Static files have nothing personal."""
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    # No framing by other pages (clickjacking), no guessing of content types, and no full
    # URLs in the Referer to other sites.
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(public)
app.include_router(router)


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    """Send people who aren't logged in to the login page, and back here afterwards.

    Only pages (GET) are come back to; after a form POST that would be a GET of a POST-only
    route. htmx requests get `HX-Redirect`, so the whole page goes to the login, instead of
    a part of the page being replaced by nothing.
    """
    back = request.url.path
    if request.method != "GET":
        back = "/"
    elif request.url.query:
        back += f"?{request.url.query}"
    url = f"/login?{urlencode({'next': back}, safe='/')}"
    if request.headers.get("HX-Request"):
        return Response(headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)
