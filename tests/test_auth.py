"""Logging in: nothing without a login, sessions, login limits and cross-site requests."""

import asyncio
import base64
import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Route

from app import deps, web
from app.config import settings
from app.main import app
from app.models import AwayPeriod, User
from app.services import auth, dishwasher, strikes, users
from app.services.login_throttle import LoginThrottle
from tests.helpers import (
    PASSWORD,
    all_routes,
    assert_no_personal_data,
    assert_sent_to_login,
    at,
    follow,
    is_logged_in,
    kitchen_pair,
    login,
    make_users,
    row_counts,
    words,
)

# Reachable without login. Everything else must send you to the login page.
PUBLIC_ROUTES = {("GET", "/login"), ("POST", "/login"), ("POST", "/logout")}


async def one_of_everything(session, people, today) -> dict:
    """A row of each kind the routes take ids of, as their path parameters."""
    pair = await kitchen_pair(session, people, today)
    later = today + timedelta(days=30)
    await users.add_away(session, people[1], later, later + timedelta(days=1), today)
    away = await session.scalar(select(AwayPeriod))
    strike = await strikes.issue(session, user_id=people[2].id, reason="x", today=today)
    clear = await dishwasher.mark_cleared(session, people[3], at(today, 12))
    await session.commit()
    return {
        "user_id": people[1].id,
        "potential_driver_id": pair.first.id,
        "away_id": away.id,
        "strike_id": strike.id,
        "clear_id": clear.id,
    }


def app_routes(ids: dict):
    """(method, route path, path with ids) of every route of the app."""
    for route in all_routes(app.routes):
        if not isinstance(route, Route):  # e.g. the /static mount
            continue
        unknown = set(route.param_convertors) - set(ids)
        assert not unknown, f"{route.path}: add an id for {unknown} to `one_of_everything`"
        for method in sorted(route.methods - {"HEAD"}):
            yield method, route.path, route.path.format(**ids)


async def test_every_route_needs_a_login(session, client, clock):
    """Every route except the login itself redirects to the login page without a session.

    Goes through `app.routes`, so routes added later are checked too. The POSTs also must
    not change anything.
    """
    people = await make_users(session, n=4)
    ids = await one_of_everything(session, people, clock.today)
    before = await row_counts(session)
    checked = set()
    for method, route, path in app_routes(ids):
        if (method, route) in PUBLIC_ROUTES:
            continue
        for headers in ({}, {"HX-Request": "true"}):
            assert_sent_to_login(await client.request(method, path, headers=headers), path, people)
        checked.add((method, route))
    # All of the app's own routes were reached, not just FastAPI's.
    own = {(m, route.path) for route in web.router.routes for m in route.methods - {"HEAD"}}
    assert own - PUBLIC_ROUTES <= checked
    assert await row_counts(session) == before

    # After logging in the same page comes back, query string included.
    for url in (f"/?cleaner={people[1].id}", "/history?until=2026-11-01"):
        path, query = url.split("?")
        back = f"{path}%3F{query.replace('=', '%3D')}"
        assert_sent_to_login(await client.get(url), back, people)
    # No API docs. The static files are public: the login page needs them.
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert (await client.get(path)).status_code == 404, path
    for path in ("/static/style.css", "/static/htmx.min.js"):
        assert (await client.get(path)).status_code == 200, path


async def test_residents_cannot_use_admin_routes(session, browser, clock):
    people = await make_users(session, n=4)
    ids = await one_of_everything(session, people, clock.today)
    before = await row_counts(session)
    resident = await browser("p3")
    admin_routes = [(m, path) for m, route, path in app_routes(ids) if route.startswith("/admin")]
    assert len(admin_routes) > 10
    for method, path in admin_routes:
        assert (await resident.request(method, path)).status_code == 403, f"{method} {path}"
    # Nor someone else's absence or duty.
    assert (await resident.post(f"/me/away/{ids['away_id']}/delete")).status_code == 404
    await resident.post(f"/potential-drivers/{ids['potential_driver_id']}/drove")
    assert await row_counts(session) == before


async def test_login_page_shows_nothing_personal(session, client, clock):
    people = await make_users(session, n=4)
    await kitchen_pair(session, people, clock.today)
    r = await client.get("/login")
    assert r.status_code == 200 and 'name="password"' in r.text
    assert "Log in with the account your floor speaker gave you" in words(r)
    assert_no_personal_data(r.text, people)
    # No links to the other pages either, they'd only lead back here. Just the brand ("/").
    links = {h for h in re.findall(r'href="([^"]*)"', r.text) if not h.startswith("/static/")}
    assert links <= {"/", "/login"}

    # Not after a failed login either, also with input no account could have.
    for username, password in (("p2", "wrong"), ("x" * 100, "x"), ("p2", "x" * 2000)):
        r = await client.post("/login", data={"username": username, "password": password})
        page = await client.get(r.headers["location"])
        assert "Wrong username or password" in words(page)
        assert_no_personal_data(page.text, people)


def session_payload(data: dict) -> bytes:
    """Session data the way Starlette's `SessionMiddleware` puts it into the cookie."""
    return base64.b64encode(json.dumps(data).encode())


def signed(data: dict, key: str | None = None) -> str:
    """A session cookie with `data`, signed with `key` (the app's by default)."""
    signer = SessionMiddleware(app=None, secret_key=key or settings.secret_key).signer
    return signer.sign(session_payload(data)).decode()


def set_session_cookie(client, value: str) -> None:
    """Replace the session cookie (not add a second one next to it)."""
    client.cookies.clear()
    client.cookies.set("rr_session", value)


async def test_forged_session_cookie_is_no_login(session, client):
    people = await make_users(session, n=4)
    admin = people[0]
    real = {"user_id": admin.id, "token": admin.session_token}
    # Control: built like this the cookie is a login, so the cases below fail because of
    # what is wrong with them, not because the cookie is malformed.
    set_session_cookie(client, signed(real))
    assert (await client.get("/admin")).status_code == 200

    for cookie in (
        signed(real, key="x" * 32),  # signed with another key
        signed({"user_id": admin.id}),  # without a session token, from before there were any
    ):
        set_session_cookie(client, cookie)
        assert_sent_to_login(await client.get("/admin"), "/admin", people)

    # A real login with the user id swapped for the floor speaker's: the signature no
    # longer matches.
    client.cookies.clear()
    await login(client, "p2")
    _, signature = client.cookies["rr_session"].split(".", 1)
    set_session_cookie(client, f"{session_payload({'user_id': admin.id}).decode()}.{signature}")
    assert_sent_to_login(await client.get("/admin"), "/admin", people)


async def test_outdated_session_is_no_login(session, browser, monkeypatch):
    people = await make_users(session, n=3)
    # A login lasts at most `SESSION_MAX_SECONDS`.
    now = [1_000_000.0]
    monkeypatch.setattr(deps, "clock", lambda: now[0])
    old = await browser("p3")
    now[0] += deps.SESSION_MAX_SECONDS - 60
    assert await is_logged_in(old)
    now[0] += 120
    assert not await is_logged_in(old)

    # The resident moved out: their session ends, and its cookie is cleared.
    client = await browser("p2")
    old_cookie, old_id = client.cookies["rr_session"], people[1].id
    await users.delete(session, people[1], settings.now())
    await session.commit()
    others = [people[0], people[2]]
    assert_sent_to_login(await client.get("/me"), "/me", others)
    assert "rr_session" not in client.cookies

    # After a reset ids start over: the old cookie doesn't log you in as whoever has that
    # id now, and logging out with it doesn't log them out.
    hash = auth.hash_password(PASSWORD)
    session.add(
        User(id=old_id, name="Someone", room_number="E120", username="else", password_hash=hash)
    )
    await session.commit()
    set_session_cookie(client, old_cookie)
    assert_sent_to_login(await client.get("/me"), "/me", others)
    theirs = await browser("else")
    set_session_cookie(client, old_cookie)
    await client.post("/logout")
    assert await is_logged_in(theirs)


async def test_logout_ends_the_session_on_every_device(session, browser):
    await make_users(session, n=3)
    laptop, phone, other_user, copy = (
        await browser("p2"),
        await browser("p2"),
        await browser("p3"),
        await browser(),
    )
    copy.cookies.set("rr_session", laptop.cookies["rr_session"])  # e.g. sniffed
    assert all([await is_logged_in(c) for c in (laptop, phone, copy, other_user)])

    await laptop.post("/logout")
    assert not any([await is_logged_in(c) for c in (laptop, phone, copy)])
    assert await is_logged_in(other_user)
    # Logging in again works, also on the other device.
    await login(phone, "p2")
    assert await is_logged_in(phone)


async def test_new_password_ends_the_other_sessions(session, browser, throttles):
    throttles(ip=3)
    people = await make_users(session, n=3)
    laptop, phone = await browser("p2"), await browser("p2")

    async def change(old, new):
        r = await laptop.post("/me/password", data={"old_password": old, "new_password": new})
        return await follow(laptop, r)

    # Whoever changed it stays logged in, everywhere else it's over.
    assert "Password changed" in await change(PASSWORD, "new-secret-1")
    assert await is_logged_in(laptop) and not await is_logged_in(phone)
    # A rejected change (wrong old password, too short) ends nothing.
    await login(phone, "p2", "new-secret-1")
    assert "Old password is wrong" in await change("wrong", "other-secret-1")
    assert "at least" in await change("new-secret-1", "short")
    assert await is_logged_in(laptop) and await is_logged_in(phone)
    # Someone with a session (an unlocked phone) can't find the password out by trying.
    for _ in range(3):
        assert "Old password is wrong" in await change("wrong", "other-secret-1")
    assert "Too many failed attempts" in await change("new-secret-1", "other-secret-1")
    await login(await browser(), "p2", "new-secret-1")  # unchanged

    # The floor speaker setting a password ends the resident's sessions, a new name doesn't.
    admin, admin_phone = await browser("p1"), await browser("p1")

    async def edit(user, name, password=""):
        data = {"name": name, "room_number": user.room_number, "password": password}
        r = await admin.post(f"/admin/users/{user.id}/edit", data=data)
        assert f"{name.split()[0]} ({user.room_number}) saved" in await follow(admin, r)

    await edit(people[1], "Renamed")
    assert await is_logged_in(laptop)
    await edit(people[1], "Renamed", "reset-pass-1")
    assert not await is_logged_in(laptop) and not await is_logged_in(phone)
    await login(laptop, "p2", "reset-pass-1")
    # Their own: this browser stays logged in, their other devices don't.
    await edit(people[0], "Person 1", "admin-pass-1")
    assert await is_logged_in(admin) and not await is_logged_in(admin_phone)


async def test_login_attempts_are_limited(session, browser, throttles, monkeypatch):
    throttles(ip=3, account=5)
    await make_users(session, n=2)

    async def attempt(client, password="wrong", username="p2"):
        r = await client.post("/login", data={"username": username, "password": password})
        return await follow(client, r)

    client = await browser()
    for _ in range(3):
        assert "Wrong username or password" in await attempt(client)
    # Blocked from this address, even with the right password; others can still log in.
    assert "Too many failed attempts" in await attempt(client, PASSWORD)
    assert not await is_logged_in(client)
    await login(client, "p1")

    # From other addresses too, with a higher limit for the account as a whole.
    for i in range(2):
        assert "Wrong username or password" in await attempt(await browser(ip=f"10.0.0.{i}"))
    assert "Too many failed attempts" in await attempt(await browser(ip="10.0.0.99"), PASSWORD)

    # Many guesses at once: each counts before its password check, not after it.
    checked = 0
    authenticate = web.auth.authenticate

    async def counting_authenticate(*args):
        nonlocal checked
        checked += 1
        await asyncio.sleep(0.01)  # like the real check, let the other requests run meanwhile
        return await authenticate(*args)

    monkeypatch.setattr(web.auth, "authenticate", counting_authenticate)
    guesser = await browser(ip="10.0.1.1")
    data = {"username": "p1", "password": "wrong"}
    await asyncio.gather(*(guesser.post("/login", data=data) for _ in range(20)))
    assert checked == 3


def test_login_throttle_window():
    now = [0.0]
    throttle = LoginThrottle(max_failures=2, window_seconds=60, clock=lambda: now[0])
    throttle.record_failure("k")
    throttle.record_failure("k")
    assert throttle.is_blocked("k")
    now[0] = 61
    assert not throttle.is_blocked("k")
    throttle.record_failure("k")
    throttle.reset("k")
    assert not throttle.is_blocked("k")

    # Old keys are forgotten, so guessing many usernames doesn't fill the memory.
    for i in range(1500):
        throttle.record_failure(f"user{i}")
    now[0] = 1000
    throttle.record_failure("fresh")
    assert len(throttle._failures) == 1


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"Origin": "http://evil.example"}, False),
        ({"Origin": "http://test:8080"}, False),  # another port is another site
        ({"Origin": "null"}, False),
        ({"Sec-Fetch-Site": "cross-site"}, False),
        ({"Sec-Fetch-Site": "same-site"}, False),  # e.g. another app on a sibling subdomain
        ({"Origin": "http://test", "Sec-Fetch-Site": "same-origin"}, True),
        ({"Sec-Fetch-Site": "none"}, True),  # typed into the address bar, a bookmark
        ({}, True),  # old browsers and scripts send neither
    ],
)
async def test_posts_only_from_this_site(session, browser, headers, allowed):
    """A page elsewhere can't make a logged-in floor speaker's browser change anything."""
    people = await make_users(session, n=3)
    admin = await browser("p1")
    target = people[2]
    data = {"name": "Changed", "room_number": target.room_number, "is_admin": "true"}
    r = await admin.post(f"/admin/users/{target.id}/edit", data=data, headers=headers)
    await session.refresh(target)
    if allowed:
        assert r.status_code == 303 and target.name == "Changed"
    else:
        assert r.status_code == 403 and target.name == "Person 3" and not target.is_admin
        # Logging in from another site (login CSRF) is refused too.
        data = {"username": "p2", "password": PASSWORD}
        assert (
            await (await browser()).post("/login", data=data, headers=headers)
        ).status_code == 403


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("/history?until=2026-11-01", "/history?until=2026-11-01"),
        *[
            (url, "/")
            for url in ("//evil.com", "/\\evil.com", "/\t/evil.com", "\\\\evil.com", "http://x")
        ],
        ("/a\r\nX: y", "/"),
    ],
)
def test_next_stays_on_the_site(url, safe):
    assert web.safe_next(url) == safe


async def test_pages_are_not_cached_and_send_security_headers(session, browser):
    await make_users(session, n=3)
    admin = await browser("p1")
    for path in ("/", "/stats", "/history", "/me", "/admin", "/help"):
        r = await admin.get(path)
        # Back after logging out must not show personal pages from the browser's cache, or
        # from htmx's (it keeps the last pages in localStorage unless told not to).
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store", path
        assert '"historyCacheSize":0' in r.text and '"refreshOnHistoryMiss":true' in r.text
    # Redirects and the login page too, they can carry flash messages.
    assert (await admin.post("/logout")).headers["cache-control"] == "no-store"
    r = await admin.get("/login")
    assert r.headers["cache-control"] == "no-store"
    # No framing by other pages, no guessing of content types, no full URLs to other sites.
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    # Static files have nothing personal and may be cached.
    assert "no-store" not in (await admin.get("/static/style.css")).headers.get("cache-control", "")
