"""Rules against a real database: assigning, deadlines, strikes, moving out, web pages."""

import random
import re
from datetime import date, datetime, time, timedelta
from fractions import Fraction

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import Duty, PotentialDriver, Status, Trip
from app.services import duties, jobs, strikes, users
from app.services.duties import DutyError
from app.services.login_throttle import LoginThrottle
from app.services.users import UserError
from app.web import person_html

MONDAY = date(2026, 11, 2)


def at(day: date, hour: int = 9) -> datetime:
    """`day` at `hour` in the app's time zone."""
    return datetime.combine(day, time(hour), settings.tz)


async def make_users(session, n=15, admin=True):
    """`n` residents p1..pn (password "secret123"); p1 is the floor speaker if `admin`."""
    created = []
    for i in range(1, n + 1):
        created.append(
            await users.create(
                session,
                name=f"Person {i}",
                room_number=f"A4{i:02d}",
                username=f"p{i}",
                password="secret123",
                is_admin=admin and i == 1,
            )
        )
    await session.commit()
    return created


async def test_tick_assigns_kitchen_and_trash(session):
    await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    trash = await duties.get_trip(session, Duty.trash, MONDAY + timedelta(days=3))
    assert len(kitchen.potential_drivers) == 2
    assert len(trash.potential_drivers) == 1
    assert trash.start_date == MONDAY and trash.end_date == MONDAY + timedelta(days=6)

    # Idempotent.
    await jobs.daily_tick(session, at(MONDAY, 13))
    assert len((await session.scalars(select(Trip))).all()) == 2


async def test_missed_kitchen_strikes_both(session):
    await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    chosen = [pd.user_id for pd in kitchen.potential_drivers]

    # Before the deadline (next day 12:00) nothing happens.
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), 11))
    assert all(pd.status is Status.pending for pd in kitchen.potential_drivers)

    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), 12))
    await session.refresh(kitchen, ["potential_drivers"])
    assert all(pd.status is Status.missed for pd in kitchen.potential_drivers)
    for uid in chosen:
        assert len(await strikes.active_strikes(session, uid)) == 1


async def test_three_strikes_give_penalty_in_next_free_week(session):
    people = await make_users(session)
    admin, a, b = people[0], people[1], people[2]
    await jobs.daily_tick(session, at(MONDAY))
    for _ in range(3):
        await strikes.issue(session, user_id=a.id, reason="x", issued_by_id=admin.id, today=MONDAY)
    for _ in range(3):
        await strikes.issue(session, user_id=b.id, reason="x", issued_by_id=admin.id, today=MONDAY)
    await session.commit()

    penalties = (
        await session.scalars(
            select(Trip).where(Trip.is_penalty.is_(True)).order_by(Trip.start_date)
        )
    ).all()
    assert [p.start_date for p in penalties] == [
        MONDAY + timedelta(days=7),
        MONDAY + timedelta(days=14),
    ]
    assert len(await strikes.active_strikes(session, a.id)) == 0

    # The penalty week replaces the regular trash duty.
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=7)))
    trip = await duties.get_trip(session, Duty.trash, MONDAY + timedelta(days=7))
    assert trip.is_penalty and [x.user_id for x in trip.potential_drivers] == [a.id]


async def test_penalty_does_not_count_as_regular_trash(session):
    people = await make_users(session, n=3)
    a = people[1]
    for _ in range(3):
        await strikes.issue(session, user_id=a.id, reason="x", today=MONDAY)
    balances = await duties.load_balances(session, Duty.trash)
    assert balances[a.id].drives == 0


async def kitchen_pair(session, people, day=MONDAY):
    """Run the daily job for `day`; returns the kitchen trip, its potential drivers, people."""
    await jobs.daily_tick(session, at(day))
    kitchen = await duties.get_trip(session, Duty.kitchen, day)
    by_id = {p.id: p for p in people}
    # Loaded like the routes do, so each potential driver keeps its trip loaded even if the
    # caller drops `kitchen` (otherwise `pd.trip` may need a lazy load, which async can't do).
    first, second = [
        await duties.get_potential_driver(session, pd.id) for pd in kitchen.potential_drivers
    ]
    return kitchen, first, second, by_id[first.user_id], by_id[second.user_id]


async def test_mark_as_driver_permissions(session):
    people = await make_users(session)
    admin = people[0]
    # Keep the admin off today's duty, so the outsider/admin checks are meaningful.
    await users.add_away(session, admin, MONDAY, MONDAY + timedelta(days=1), MONDAY)
    _, first, second, p1, p2 = await kitchen_pair(session, people)
    outsider = next(p for p in people if p.id not in {p1.id, p2.id} and not p.is_admin)

    with pytest.raises(DutyError):
        await duties.mark_as_driver(session, first, outsider, at(MONDAY, 20))
    # Nobody can mark someone else's duty.
    with pytest.raises(DutyError):
        await duties.mark_as_driver(session, first, p2, at(MONDAY, 20))

    # Too late for a resident, but the floor speaker can still do it.
    tuesday = MONDAY + timedelta(days=1)
    _, t_first, _, t1, _ = await kitchen_pair(session, people, tuesday)
    with pytest.raises(DutyError):
        await duties.mark_as_driver(session, t_first, t1, at(tuesday + timedelta(days=1), 12))
    await duties.mark_as_driver(session, t_first, admin, at(tuesday + timedelta(days=1), 12))
    assert t_first.status is Status.drove


async def test_first_driver_closes_the_trip(session):
    people = await make_users(session)
    kitchen, first, second, p1, p2 = await kitchen_pair(session, people)

    await duties.mark_as_driver(session, second, p2, at(MONDAY, 20))
    assert second.status is Status.drove and second.completed_by_id == p2.id
    assert first.status is Status.rode
    with pytest.raises(DutyError):
        await duties.mark_as_driver(session, first, p1, at(MONDAY, 21))
    await session.commit()

    # Only the one who cleaned gets credit; the partner gets neither credit nor strike.
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p2.id].drives == 1 and balances[p1.id].drives == 0
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=2)))
    assert await strikes.active_strikes(session, p1.id) == []


async def test_undriven_trip_is_shared_for_choosing_but_not_for_score(session):
    people = await make_users(session)
    _, _, _, p1, p2 = await kitchen_pair(session, people)
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p1.id].pending_share == balances[p2.id].pending_share == Fraction(1, 2)
    # The score shown only changes once someone drives.
    assert balances[p1.id].drives == balances[p2.id].drives == 0

    # Tomorrow is chosen before anyone drove today; today's pair isn't chosen again.
    tomorrow = await duties.materialize(session, Duty.kitchen, MONDAY + timedelta(days=1))
    assert {pd.user_id for pd in tomorrow.potential_drivers}.isdisjoint({p1.id, p2.id})


async def test_trip_nobody_drove_does_not_count(session):
    """A trip nobody drove (missed, excused, disputed) counts for nobody."""
    people = await make_users(session)
    await kitchen_pair(session, people)
    # Monday's and Tuesday's kitchen duty are missed by Wednesday afternoon.
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1)))
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=2), 13))

    balances = await duties.load_balances(session, Duty.kitchen)
    assert all(b.score == 0 for b in balances.values())


async def test_scores_sum_to_zero(session):
    """All scores sum to 0: what the driver is ahead, the riders are behind."""
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, _, p1, _ = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))

    balances = await duties.load_balances(session, Duty.kitchen)
    assert sum(b.score for b in balances.values()) == 0
    assert balances[p1.id].score == 1 - Fraction(1, 14)


async def test_disputed_trip_stops_counting(session):
    """A disputed trip wasn't driven after all, so it counts for nobody."""
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, _, p1, _ = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))

    await duties.dispute(session, first, admin, at(MONDAY, 21))
    balances = await duties.load_balances(session, Duty.kitchen)
    assert all(b.score == 0 for b in balances.values())


async def test_preview_has_one_plan_per_open_potential_driver(session):
    people = await make_users(session)
    _, first, _, p1, p2 = await kitchen_pair(session, people)

    async def plans():
        preview = await duties.preview(
            session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=30)
        )
        return preview.plans

    before = await plans()
    assert set(before) == {p1.id, p2.id}
    # Whoever doesn't clean is up again tomorrow; whoever does, much later.
    for driver, other in [(p1, p2), (p2, p1)]:
        plan = before[driver.id]
        assert other.id in plan[0].potential_drivers
        assert all(driver.id not in e.potential_drivers for e in plan[:7])

    # Once it's done, the outcome is known: a single plan.
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))
    after = await plans()
    assert list(after) == [None]
    assert after[None] == before[p1.id]


async def test_partner_who_did_not_drive_is_chosen_next(session):
    people = await make_users(session)
    _, first, _, p1, p2 = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))
    tomorrow = await duties.materialize(session, Duty.kitchen, MONDAY + timedelta(days=1))
    ids = {pd.user_id for pd in tomorrow.potential_drivers}
    assert p2.id in ids and p1.id not in ids


async def test_dispute_gives_strike_and_removes_the_drive(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, second, p1, p2 = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))

    with pytest.raises(DutyError):
        await duties.dispute(session, first, p2, at(MONDAY, 21))
    await duties.dispute(session, first, admin, at(MONDAY, 21))
    await session.commit()

    assert first.status is Status.missed and first.disputed_at is not None
    assert second.status is Status.rode
    [strike] = await strikes.active_strikes(session, p1.id)
    assert strike.issued_by_id == admin.id and strike.potential_driver_id == first.id
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p1.id].drives == 0
    # The deadline passing later doesn't add a second strike.
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=2)))
    assert len(await strikes.active_strikes(session, p1.id)) == 1


async def test_admin_marking_missed_driver_revokes_strike(session):
    people = await make_users(session)
    admin = people[0]
    await jobs.daily_tick(session, at(MONDAY))
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), 13))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    pd = await duties.get_potential_driver(session, kitchen.potential_drivers[0].id)
    assert pd.status is Status.missed
    await duties.mark_as_driver(session, pd, admin, at(MONDAY + timedelta(days=2)))
    assert len(await strikes.active_strikes(session, pd.user_id)) == 0


async def test_away_replaces_pending_potential_driver(session):
    people = await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    leaving_id = kitchen.potential_drivers[0].user_id
    leaving = next(p for p in people if p.id == leaving_id)

    await users.add_away(session, leaving, MONDAY, MONDAY + timedelta(days=3), MONDAY)
    await session.commit()
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    ids = [pd.user_id for pd in kitchen.potential_drivers]
    assert len(ids) == 2 and leaving_id not in ids

    preview = await duties.preview(
        session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=3)
    )
    assert all(
        leaving_id not in e.potential_drivers for plan in preview.plans.values() for e in plan
    )


async def test_delete_user_keeps_history_and_replaces(session):
    people = await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    trash = await duties.get_trip(session, Duty.trash, MONDAY)
    gone_id = trash.potential_drivers[0].user_id
    gone = next(p for p in people if p.id == gone_id)
    await users.delete(session, gone, MONDAY)
    await session.commit()
    trash = await duties.get_trip(session, Duty.trash, MONDAY)
    assert [pd.user_id for pd in trash.potential_drivers] != [gone_id]
    assert len(trash.potential_drivers) == 1


async def test_month_of_ticks_is_fair(session):
    """30 days, one random person of each pair cleans: everybody cleans 2 times."""
    rng = random.Random(7)
    await make_users(session)
    day = MONDAY
    for _ in range(30):
        await jobs.daily_tick(session, at(day))
        trip = await duties.get_trip(session, Duty.kitchen, day)
        driver = await duties.get_potential_driver(session, rng.choice(trip.potential_drivers).id)
        await duties.mark_as_driver(session, driver, driver.user, at(day, 20))
        await session.commit()
        day += timedelta(days=1)
    rows = (
        (
            await session.execute(
                select(PotentialDriver.user_id)
                .join(PotentialDriver.trip)
                .where(Trip.duty == Duty.kitchen, PotentialDriver.status == Status.drove)
            )
        )
        .scalars()
        .all()
    )
    counts = [rows.count(u) for u in set(rows)]
    assert len(set(rows)) == 15 and max(counts) - min(counts) <= 1


@pytest.fixture
async def client(session_factory):
    """HTTP client that calls the app directly (no server), keeping the login cookie."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def login(client, username):
    """Log in as `username` with the test password."""
    r = await client.post("/login", data={"username": username, "password": "secret123"})
    assert r.status_code == 303


async def test_web_permissions(session, client):
    await make_users(session, n=3)
    r = await client.get("/me")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = await client.get("/")
    assert r.status_code == 200 and "Kitchen duty" in r.text

    await login(client, "p2")
    assert (await client.get("/admin")).status_code == 403
    assert (await client.get("/me")).status_code == 200

    await client.post("/logout")
    await login(client, "p1")
    assert (await client.get("/admin")).status_code == 200
    r = await client.post(
        "/admin/users",
        data={"name": "Neu", "room_number": "A420", "username": "neu", "password": "pw123456"},
    )
    assert r.status_code == 303
    assert "Neu" in (await client.get("/admin")).text


async def test_help_page(session, client):
    await make_users(session, n=3)
    r = await client.get("/help")
    assert r.status_code == 200 and "Kitchen duty" in r.text
    assert "For the floor speaker" not in r.text

    await login(client, "p2")
    assert "For the floor speaker" not in (await client.get("/help")).text
    await client.post("/logout")
    await login(client, "p1")
    assert "For the floor speaker" in (await client.get("/help")).text


async def test_understaffed_trip_is_topped_up(session):
    # The tick ran before anyone lived on the floor.
    await jobs.daily_tick(session, at(MONDAY))
    assert (await duties.get_trip(session, Duty.kitchen, MONDAY)).potential_drivers == []

    await make_users(session, n=5)
    await jobs.daily_tick(session, at(MONDAY, 10))
    assert len((await duties.get_trip(session, Duty.kitchen, MONDAY)).potential_drivers) == 2
    assert len((await duties.get_trip(session, Duty.trash, MONDAY)).potential_drivers) == 1


async def test_history_page_and_dispute(session, client):
    people = await make_users(session, n=4)
    _, first, _, p1, _ = await kitchen_pair(session, people, settings.today())
    await duties.mark_as_driver(session, first, p1, settings.now())
    await session.commit()

    assert (await client.get("/history")).status_code == 303  # login required
    await login(client, "p2")
    r = await client.get("/history")
    assert r.status_code == 200 and str(person_html(p1)) in r.text
    assert ">Dispute</button>" not in r.text
    assert (await client.post(f"/admin/potential-drivers/{first.id}/dispute")).status_code == 403

    await client.post("/logout")
    await login(client, "p1")
    r = await client.get("/history")
    assert ">Dispute</button>" in r.text
    r = await client.post(f"/admin/potential-drivers/{first.id}/dispute?next=/history")
    assert r.status_code == 303
    assert ">disputed<" in (await client.get("/history")).text


async def test_top_up_skips_trip_that_is_already_driven(session):
    # Only one resident: they get today's kitchen duty alone and do it.
    [first] = await make_users(session, n=1)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    a = await duties.get_potential_driver(session, kitchen.potential_drivers[0].id)
    await duties.mark_as_driver(session, a, first, at(MONDAY, 10))
    await session.commit()

    # A resident added later must not be put on the driven trip (and struck for it).
    newbie = await users.create(
        session, name="Neu", room_number="A420", username="neu", password="secret123"
    )
    await duties.fill_current(session, MONDAY)
    await session.commit()
    kitchen = await duties.get_trip(session, Duty.kitchen, MONDAY)
    assert [x.user_id for x in kitchen.potential_drivers] == [first.id]

    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), 13))
    assert await strikes.active_strikes(session, newbie.id) == []


async def test_web_errors_do_not_break_the_page(session, client):
    people = await make_users(session, n=4)
    _, first, _, p1, _ = await kitchen_pair(session, people, settings.today())
    outsider = next(p for p in people if p.id != p1.id and not p.is_admin)

    # Clicking someone else's card button reloads the page instead of nesting it in the card.
    await login(client, outsider.username)
    r = await client.post(
        f"/potential-drivers/{first.id}/drove?card=1", headers={"HX-Request": "true"}
    )
    assert r.status_code == 200 and r.headers["HX-Refresh"] == "true"

    await client.post("/logout")
    await login(client, "p1")
    r = await client.post("/admin/potential-drivers/999999/excuse")
    assert r.status_code == 303 and r.headers["location"] == "/admin"


async def test_login_is_blocked_after_too_many_failures(session, client, monkeypatch):
    monkeypatch.setattr("app.web.login_throttle", LoginThrottle(max_failures=3))
    await make_users(session, n=2)
    for _ in range(3):
        r = await client.post("/login", data={"username": "p2", "password": "wrong"})
        assert "Wrong username or password" in (await client.get(r.headers["location"])).text

    # Even the right password is refused while blocked.
    r = await client.post("/login", data={"username": "p2", "password": "secret123"})
    assert "Too many failed attempts" in (await client.get(r.headers["location"])).text
    assert (await client.get("/me")).status_code == 303
    # Other users can still log in.
    await login(client, "p1")
    assert (await client.get("/me")).status_code == 200


async def test_short_passwords_are_rejected(session):
    with pytest.raises(UserError):
        await users.create(session, name="A", room_number="A401", username="a", password="short")


def test_room_numbers():
    assert users.check_room(" a403 ") == "A403"
    for valid in ["A401", "A499", "B110"]:
        assert users.check_room(valid) == valid
    for invalid in ["A40", "A400", "A.421", "A4.1", "A4100", "4403", "AA403", ""]:
        with pytest.raises(UserError):
            users.check_room(invalid)


async def test_initial_admin_only_on_empty_database(session, monkeypatch):
    monkeypatch.setattr(settings, "admin_username", "")  # whatever the local .env says
    assert await users.create_initial_admin(session) is None
    for key, value in dict(username="Chef", password="secret123", name="Chef", room="A401").items():
        monkeypatch.setattr(settings, f"admin_{key}", value)
    admin = await users.create_initial_admin(session)
    assert admin.is_admin and admin.username == "chef"
    await session.commit()
    # Not again once there are users, e.g. after the admin was deleted and others were added.
    assert await users.create_initial_admin(session) is None


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


async def test_moving_out_does_not_change_the_others_balance(session):
    people = await make_users(session, n=5)
    day = MONDAY
    for _ in range(10):
        await jobs.daily_tick(session, at(day))
        trip = await duties.get_trip(session, Duty.kitchen, day)
        driver = await duties.get_potential_driver(session, trip.potential_drivers[0].id)
        await duties.mark_as_driver(session, driver, driver.user, at(day, 20))
        await session.commit()
        day += timedelta(days=1)

    leaving, staying = people[1], [p for p in people if p is not people[1]]
    staying_ids = [p.id for p in staying]
    before = await duties.load_balances(session, Duty.kitchen)
    # Move out on the last scheduled day, so no new trip changes the balances.
    await users.delete(session, leaving, day - timedelta(days=1))
    await session.commit()
    after = await duties.load_balances(session, Duty.kitchen)
    assert [after[uid].score for uid in staying_ids] == [before[uid].score for uid in staying_ids]

    # The new resident of that room starts with a clean slate.
    newbie = await users.create(
        session, name="Neu", room_number=leaving.room_number, username="neu", password="secret123"
    )
    await session.commit()
    balances = await duties.load_balances(session, Duty.kitchen)
    assert newbie.id not in balances
    assert await strikes.active_strikes(session, newbie.id) == []


async def test_moving_out_drops_open_penalty_week(session):
    people = await make_users(session)
    leaving = people[1]
    await jobs.daily_tick(session, at(MONDAY))
    for _ in range(3):
        await strikes.issue(session, user_id=leaving.id, reason="x", today=MONDAY)
    await session.commit()

    await users.delete(session, leaving, MONDAY)
    await session.commit()
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=7)))
    trip = await duties.get_trip(session, Duty.trash, MONDAY + timedelta(days=7))
    assert not trip.is_penalty and len(trip.potential_drivers) == 1


async def test_own_name_is_highlighted(session, client):
    people = await make_users(session, n=3)
    await login(client, "p2")
    text = (await client.get("/stats")).text
    assert str(person_html(people[1], is_me=True)) in text
    assert str(person_html(people[0])) in text


async def test_stats_shows_whole_points(session, client):
    """Scores 2/3, -1/3, -1/3 are shown as whole points that still sum to 0."""
    people = await make_users(session, n=3)
    _, first, _, p1, _ = await kitchen_pair(session, people)
    await duties.mark_as_driver(session, first, p1, at(MONDAY, 20))
    await session.commit()
    text = (await client.get("/stats")).text
    cells = re.findall(r'<td class="points">([^<]*)</td>', text)
    kitchen = [int(c.replace("\u2212", "-")) for c in cells[::2]]
    assert sorted(kitchen) == [-34, -33, 67]
    assert all(c == "0" for c in cells[1::2])


async def test_dashboard_switches_between_plans(session, client):
    people = await make_users(session, n=6)
    _, _, _, p1, p2 = await kitchen_pair(session, people, settings.today())
    await session.commit()

    def current(text):
        return re.findall(r'cleaner=(\d+)"[^>]*\n?[^>]*aria-current="true"', text)

    text = (await client.get("/")).text
    assert "Depends on who cleans today" in text
    assert current(text) == [str(p1.id)]
    text = (await client.get(f"/?cleaner={p2.id}")).text
    assert current(text) == [str(p2.id)]
    # Unknown ids fall back to the default; logged in as one of the pair, that's yourself.
    await login(client, p2.username)
    assert current((await client.get("/?cleaner=999")).text) == [str(p2.id)]
    assert "If you clean" in (await client.get("/")).text


async def test_stats_colour_strikes(session, client):
    people = await make_users(session, n=3)
    await strikes.issue(session, user_id=people[1].id, reason="x", today=MONDAY)
    for _ in range(2):
        await strikes.issue(session, user_id=people[2].id, reason="x", today=MONDAY)
    await session.commit()
    text = (await client.get("/stats")).text
    assert '"strikes ok">0 / 3' in text
    assert '"strikes warn">1 / 3' in text
    assert '"strikes bad">2 / 3' in text
