"""Test setup: a separate `e1_test` database, recreated empty for every test."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://e1:e1@localhost:5432/e1_test")
os.environ["RUN_SCHEDULER"] = "false"
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-0123456789")
# The rules as the defaults have them, whatever a local .env says (env vars win over .env).
os.environ.update(
    TIMEZONE="Europe/Berlin",
    DEADLINE_HOUR="8",
    KITCHEN_PEOPLE_PER_DAY="2",
    TRASH_PEOPLE_PER_WEEK="1",
    STRIKE_THRESHOLD="3",
    PREVIEW_DAYS="14",
    DISHWASHER_COOLDOWN_MINUTES="90",
    DISHWASHER_HEROES="2",
)

from contextlib import AsyncExitStack  # noqa: E402

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services import login_throttle  # noqa: E402
from app.services.login_throttle import (  # noqa: E402
    ACCOUNT_MAX_FAILURES,
    MAX_FAILURES,
    LoginThrottle,
)
from tests.helpers import WEDNESDAY, at, login  # noqa: E402

# The fixtures drop the whole schema: never let an exported DATABASE_URL point them at
# the development or production database.
if not make_url(settings.database_url).database.endswith("_test"):
    raise RuntimeError(
        f"Tests need a database whose name ends in _test, not {settings.database_url!r}"
    )


async def ensure_test_database() -> None:
    """Create the test database if it doesn't exist yet."""
    url = make_url(settings.database_url)
    params = dict(user=url.username, password=url.password, host=url.host, port=url.port)
    try:
        conn = await asyncpg.connect(**params, database=url.database)
    except asyncpg.InvalidCatalogNameError:
        conn = await asyncpg.connect(**params, database="postgres")
        await conn.execute(f'CREATE DATABASE "{url.database}"')
    await conn.close()


@pytest.fixture
async def session_factory(monkeypatch):
    """Fresh tables; the app's own sessions use the test database too. Skips without DB."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        await ensure_test_database()
        async with engine.begin() as conn:
            # The whole schema, not `drop_all`: that misses tables the models no longer have
            # (e.g. after a rename), which still reference `users`.
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.run_sync(Base.metadata.create_all)
    except OSError:
        if os.environ.get("CI"):  # in CI a missing database is an error, not a skip
            raise
        pytest.skip("test database not reachable (docker compose up -d db)")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(session_factory):
    """A database session for the test."""
    async with session_factory() as s:
        yield s


@pytest.fixture(autouse=True)
def throttles(monkeypatch):
    """Fresh login limits for every test; call it to set lower ones."""

    def set_limits(ip=MAX_FAILURES, account=ACCOUNT_MAX_FAILURES):
        monkeypatch.setattr(login_throttle, "ip_throttle", LoginThrottle(max_failures=ip))
        monkeypatch.setattr(login_throttle, "account_throttle", LoginThrottle(max_failures=account))

    set_limits()
    return set_limits


class Clock:
    """The app's clock (`settings.now`), fixed so pages don't depend on the real date."""

    def __init__(self, monkeypatch):
        self.now = at(WEDNESDAY, 12)  # after the deadline hour, in the middle of a week
        monkeypatch.setattr(type(settings), "now", lambda _: self.now)

    @property
    def today(self):
        return self.now.date()

    def set(self, day, hour=12, minute=0):
        self.now = at(day, hour, minute)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    return Clock(monkeypatch)


@pytest.fixture
async def browser(session_factory, clock):
    """Opens browsers on the app (no server), each with its own cookies.

    `await browser("p2")` is logged in as p2; `ip` is the address it connects from.
    """
    async with AsyncExitStack() as stack:

        async def open_browser(username=None, ip="127.0.0.1"):
            transport = httpx.ASGITransport(app=app, client=(ip, 1234))
            client = await stack.enter_async_context(
                httpx.AsyncClient(transport=transport, base_url="http://test")
            )
            if username:
                await login(client, username)
            return client

        yield open_browser


@pytest.fixture
async def client(browser):
    """A browser that isn't logged in."""
    return await browser()
