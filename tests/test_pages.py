"""The pages: what they say to whom, and their buttons."""

import html
import re
from datetime import timedelta

from sqlalchemy import func, select

from app.config import settings
from app.formatting import long_date, short_date
from app.models import AwayPeriod, DishwasherClear, Duty, PotentialDriver, Status, User
from app.services import dishwasher, duties, strikes, users
from app.web import person_html
from tests.helpers import (
    MONDAY,
    TUESDAY,
    at,
    follow,
    give_strikes,
    kitchen_pair,
    make_users,
    plans,
    section,
    words,
)

CARD = {"HX-Request": "true"}


def you(page: str) -> str:
    """The "you" line at the top of the dashboard and the stats page."""
    return page.split('id="you"')[1].split("</p>")[0]


def rows(part: str) -> list[str]:
    """The table rows in `part` of a page, as HTML."""
    return re.findall(r"<tr[^>]*>(.*?)</tr>", part, re.S)


def excuse(pd) -> str:
    return f"/admin/potential-drivers/{pd.id}/excuse"


# --- dashboard ----------------------------------------------------------------


async def test_dashboard_for_those_on_duty_and_the_others(session, browser, clock, monkeypatch):
    people = await make_users(session, n=6)
    pair = await kitchen_pair(session, people, clock.today)
    trash = await duties.get_trip(session, Duty.trash, clock.today)
    busy = pair.ids | {pd.user_id for pd in trash.potential_drivers}
    outsider = next(p for p in people if p.id not in busy and not p.is_admin)

    on_duty = await browser(pair.p1.username)
    page = (await on_duty.get("/")).text
    assert "You're on kitchen duty today" in words(you(page))
    assert "New here?" not in words(you(page))
    # The card says what to do and by when, to the pair. Your own button says what it
    # means; it isn't the "done" status word.
    assert "Only one of you needs to clean" in words(page)
    assert "taps “I cleaned it” before" in words(page)
    assert ">I cleaned it</button>" in page and ">Done</button>" not in page

    free = await browser(outsider.username)
    page = (await free.get("/")).text
    line = words(you(page))
    assert "You're free today" in line and "Next kitchen:" in line and "Next trash:" in line
    assert "strike" not in line and "Only one of them needs to clean" in words(page)
    # Someone who has never been on a duty gets pointed to the explanation.
    assert "New here?" in line and 'href="/help"' in you(page)
    await strikes.issue(session, user_id=outsider.id, reason="test", today=clock.today)
    await session.commit()
    line = you((await free.get("/")).text)
    threshold = settings.strike_threshold
    assert f"1 of {threshold} strikes" in words(line) and 'href="/me"' in line
    assert f"at {threshold}: an extra trash week" in words(line)

    # After cleaning: thanks, also when you're on trash duty this week as well.
    await on_duty.post(f"/potential-drivers/{pair.first.id}/drove")
    assert "You did kitchen duty today – thanks!" in words(you((await on_duty.get("/")).text))
    # Now the plan is known, the next kitchen duty says with whom. (The app changed the
    # duty in its own session; this one forgets what it loaded before.)
    by_id = {p.id: (p.username, p.room_number) for p in people}
    session.expire_all()
    me, partner = (await plans(session, clock.today))[None][0].potential_drivers
    line = words(you((await (await browser(by_id[me][0])).get("/")).text))
    assert f"with Person {by_id[partner][1]}" in line

    monkeypatch.setattr(settings, "preview_days", 0)
    assert (await free.get("/")).status_code == 200


async def test_done_and_undo_on_the_card(session, browser, clock):
    people = await make_users(session, n=6)
    pair = await kitchen_pair(session, people, clock.today, admin_off=True)
    done = f"/potential-drivers/{pair.first.id}/drove?card=1"
    undo = f"/potential-drivers/{pair.first.id}/undo?card=1"
    # Someone else's button (on an old page) reloads the page instead of nesting an error
    # page in the card.
    outsider = next(p for p in people[1:] if p.id not in pair.ids)
    r = await (await browser(outsider.username)).post(done, headers=CARD)
    assert r.status_code == 200 and r.headers["HX-Refresh"] == "true"

    # htmx follows to the dashboard and takes the card and the previews from it.
    mine = await browser(pair.p1.username)
    r = await mine.post(done, headers=CARD, follow_redirects=True)
    assert 'id="card-kitchen"' in r.text and 'id="kitchen-preview"' in r.text
    assert "The plan depends on" not in r.text and f'hx-post="{undo}"' in r.text
    # The partner sees no Undo for someone else's Done, and can't undo it.
    partner = await browser(pair.p2.username)
    assert undo not in (await partner.get("/")).text
    assert (await partner.post(undo, headers=CARD)).headers["HX-Refresh"] == "true"
    # Undone: the buttons are back, and the plan depends on who cleans again.
    r = await mine.post(undo, headers=CARD, follow_redirects=True)
    assert undo not in r.text and done in r.text and "The plan depends on" in r.text

    # The floor speaker can undo a Done on the admin page.
    admin = await browser("p1")
    await admin.post(f"/potential-drivers/{pair.first.id}/drove?next=/admin")
    form = f'action="/potential-drivers/{pair.first.id}/undo?next=/admin'
    assert form in (await admin.get("/admin")).text
    r = await admin.post(f"/potential-drivers/{pair.first.id}/undo?next=/admin")
    assert r.headers["location"] == "/admin"
    page = await admin.get("/admin")
    assert "Undone: the duty is open again" in words(page) and form not in page.text


def current_plan(page: str) -> list[str]:
    """Whose plan the switch shows (as ids)."""
    return re.findall(r'cleaner=(\d+)"[^>]*\n?[^>]*aria-current="true"', page)


async def test_plan_switch(session, browser, clock):
    people = await make_users(session, n=6)
    pair = await kitchen_pair(session, people, clock.today)
    other = await browser(next(p for p in people if p.id not in pair.ids).username)
    page = (await other.get("/")).text
    assert "The plan depends on who cleans today" in words(page)
    # Not on the duty yourself, the first one's plan is the default.
    assert current_plan(page) == [str(pair.p1.id)]
    for driver, partner in [(pair.p1, pair.p2), (pair.p2, pair.p1)]:
        page = (await other.get(f"/?cleaner={driver.id}", headers=CARD)).text
        assert current_plan(page) == [str(driver.id)]
        # Whoever doesn't clean is up again tomorrow.
        tomorrow = words(rows(section(page, "kitchen-preview"))[0])
        assert partner.room_number in tomorrow and driver.room_number not in tomorrow

    # On the duty, your own plan is the default; anything that isn't one of the pair falls
    # back to it, instead of an error page.
    mine = await browser(pair.p2.username)
    for cleaner in ("", "999", "abc", "²", "9" * 5000):
        r = await mine.get(f"/?cleaner={cleaner}")
        assert r.status_code == 200 and current_plan(r.text) == [str(pair.p2.id)], cleaner
    # The buttons only switch the preview; they don't report anything.
    assert "Plan if you clean" in words(r) and "only changes the preview" in words(r)

    # Switching takes only a part of the page: messages wait for the next whole page.
    admin = await browser("p1")
    await admin.post("/admin/tick")  # queues "Daily job done"
    assert "Daily job done" not in (await admin.get("/", headers=CARD)).text
    page = await admin.get("/", headers=CARD | {"HX-Boosted": "true"})
    assert "Daily job done" in page.text


async def test_dashboard_before_the_deadline_hour(session, browser, clock):
    """Before the deadline hour the card still shows yesterday's duty, and says so."""
    people = await make_users(session, n=6)
    pair = await kitchen_pair(session, people, MONDAY)
    clock.set(TUESDAY, 3)
    hour = settings.deadline_hour

    line = words(you((await (await browser(pair.p2.username)).get("/")).text))
    assert "Your kitchen duty from yesterday is still open" in line
    assert f"by {hour:02d}:00" in line
    # Whoever cleaned it decides whether you're up again today: the earliest outcome first.
    assert f"Next kitchen: today from {hour:02d}:05 if Person {pair.p1.room_number} cleans," in line
    assert "otherwise" in line

    outsider = next(p for p in people if p.id not in pair.ids)
    page = await (await browser(outsider.username)).get("/")
    assert "yesterday" in words(section(page, "card-kitchen"))
    assert "The plan depends on who cleaned yesterday" in words(page)
    assert f"Today's kitchen duty is assigned at {hour:02d}:05" in words(page)
    assert await duties.get_trip(session, Duty.kitchen, TUESDAY) is None


async def test_dishwasher_pages(session, browser, clock, monkeypatch):
    people = await make_users(session, n=4)
    # p3 is a hero from last month, shown this month.
    last_month = dishwasher.previous_month(clock.today).replace(day=15)
    await dishwasher.mark_cleared(session, people[2], at(last_month, 12))
    await session.commit()

    p2 = await browser("p2")
    home = await p2.get("/")
    assert 'hx-post="/dishwasher?card=1"' in home.text
    assert "has no kitchen duty in" in words(home) and "dishwasher hero" in words(home)
    # Says what it's for to someone who hasn't emptied it yet.
    assert "Optional: the 2 who empty it most get next month off kitchen duty" in words(home)
    # The card swaps itself, like "Done".
    r = await p2.post("/dishwasher?card=1", headers=CARD)
    assert r.status_code == 303 and r.headers["location"] == "/"
    home = await p2.get("/")
    # While it can't be marked: a plain label saying when it can, no (disabled) button.
    assert "Again from" in words(home) and "disabled" not in home.text
    assert 'hx-post="/dishwasher?card=1"' not in home.text
    # A mis-tap can be taken back for a few minutes, by whoever marked it.
    clear_id = await session.scalar(
        select(DishwasherClear.id).where(DishwasherClear.user_id == people[1].id)
    )
    undo = f'hx-post="/dishwasher/{clear_id}/undo?card=1"'
    assert undo in home.text
    assert "You emptied it once this month" in words(home) and "Optional:" not in words(home)
    assert "Last emptied by you today" in words(home)
    # Too soon: the page reloads to show why.
    r = await p2.post("/dishwasher?card=1", headers=CARD)
    assert r.headers.get("hx-refresh") == "true"
    assert "already emptied by you" in words(await p2.get("/"))
    stats = words(await p2.get("/stats"))
    assert stats.count(f"on track for {dishwasher.next_month(clock.today):%B}") == 1
    assert "You emptied it once this month" in words(await p2.get("/me"))

    # The hero is addressed directly; someone else emptied it just now.
    p3 = await browser("p3")
    home = await p3.get("/")
    assert "You have no kitchen duty in" in words(home) and "Again from" in words(home)
    assert undo not in home.text
    r = await p3.post(f"/dishwasher/{clear_id}/undo?card=1", headers=CARD)
    assert r.headers.get("hx-refresh") == "true"
    assert "You have no kitchen duty in" in words(await p3.get("/me"))

    await users.set_active(session, people[3], False, clock.now)
    await session.commit()
    assert "inactive" in words(section(await (await browser("p4")).get("/"), "card-dishwasher"))

    admin = await browser("p1")
    assert f"/admin/dishwasher/{clear_id}/delete" in (await admin.get("/admin")).text
    assert (await admin.post(f"/admin/dishwasher/{clear_id}/delete")).status_code == 303
    assert await session.scalar(select(func.count(DishwasherClear.id))) == 1
    # Without htmx the whole page comes back with a message.
    monkeypatch.setattr(settings, "dishwasher_cooldown_minutes", 0)
    assert "Marked as emptied" in await follow(admin, await admin.post("/dishwasher"))


# --- other pages ----------------------------------------------------------------


async def test_stats_page(session, browser, clock):
    people = await make_users(session, n=4)
    admin = people[0]
    pair = await kitchen_pair(session, people, clock.today, admin_off=True)
    await duties.mark_as_driver(session, pair.first, pair.p1, clock.now)
    other = next(p for p in people[1:] if p.id not in pair.ids)
    await give_strikes(session, pair.p1.id, 1, clock.today)
    await give_strikes(session, pair.p2.id, 2, clock.today)
    [revoked] = await give_strikes(session, other.id, 1, clock.today)
    await strikes.revoke(session, revoked, clock.now)
    await session.commit()

    page = (await (await browser(pair.p1.username)).get("/stats")).text
    # p1 cleaned once with 3 riders: two thirds of a duty ahead in the kitchen.
    line = words(you(page))
    assert "Kitchen: +0.7 duties (more than your share)" in line and "Trash: on par" in line
    assert f"Strikes: 1 of {settings.strike_threshold}" in line
    assert "+100 points = one duty more than your fair share" in words(page)
    # Whole points that still sum to 0, and the active strikes, per resident. Your own
    # name is highlighted.
    table = page.split('<table class="stats">')[1].split("</table>")[0]
    cells = {row.split()[1]: row.split()[2:5] for row in map(words, rows(table)[1:])}
    kitchen, trash, struck = ({u.id: cells[u.room_number][i] for u in people} for i in range(3))
    assert kitchen[pair.p1.id] == "+67" and kitchen[admin.id] == "0"
    assert sorted([kitchen[pair.p2.id], kitchen[other.id]]) == ["−33", "−34"]
    assert set(trash.values()) == {"0"}
    assert struck == {admin.id: "0", pair.p1.id: "1", pair.p2.id: "2", other.id: "0"}
    assert str(person_html(pair.p1, is_me=True)) in page and str(person_html(admin)) in page


async def test_history_page(session, browser, clock):
    people = await make_users(session, n=4)
    pair = await kitchen_pair(session, people, clock.today, admin_off=True)
    await duties.mark_as_driver(session, pair.first, pair.p1, clock.now)
    await session.commit()

    resident = await browser(pair.p2.username)
    page = (await resident.get("/history")).text
    assert str(person_html(pair.p1)) in page and ">Dispute</button>" not in page
    for until in ("", "0001-01-01", "garbage"):
        assert (await resident.get(f"/history?until={until}")).status_code == 200, until

    admin = await browser("p1")
    assert ">Dispute</button>" in (await admin.get("/history")).text
    r = await admin.post(f"/admin/potential-drivers/{pair.first.id}/dispute?next=/history")
    assert r.headers["location"] == "/history"
    assert ">disputed<" in (await admin.get("/history")).text


async def test_profile_page(session, browser, clock):
    people = await make_users(session, n=4)
    pair = await kitchen_pair(session, people, clock.today)
    me = await browser(pair.p1.username)
    # An absence over a duty you already have: it stays yours, and the page says so.
    today = clock.today.isoformat()
    page = await follow(
        me, await me.post("/me/away", data={"start_date": today, "end_date": today})
    )
    assert "Away period added" in page and "stays with you" in page
    assert "Duties you already have stay yours" in page
    assert f"At {settings.strike_threshold} strikes you get a penalty trash week" in page
    assert 'hx-confirm="Delete your absence from' in html.unescape((await me.get("/me")).text)


async def test_help_page(session, browser):
    await make_users(session, n=3)
    page = words(await (await browser("p2")).get("/help"))
    assert "Kitchen duty" in page and "For the floor speaker" not in page
    # The pair rule spelled out, and who the floor speaker is.
    assert "Agree who cleans" in page and "you both get a strike" in page
    assert "Your floor speaker: Person" in page
    assert "For the floor speaker" in words(await (await browser("p1")).get("/help"))


# --- admin ----------------------------------------------------------------------


async def test_admin_needs_attention_and_keep_missed(session, browser, clock):
    people = await make_users(session, n=4)
    admin = await browser("p1")
    assert "Nothing needs attention." in words(await admin.get("/admin"))

    # Yesterday's kitchen duty nobody marked (missed by now), and today's (still open).
    late = await kitchen_pair(session, people, clock.today - timedelta(days=1))
    now = await kitchen_pair(session, people, clock.today)
    trash = await duties.get_trip(session, Duty.trash, clock.today)
    await give_strikes(session, people[3].id, settings.strike_threshold - 1, clock.today)
    await session.commit()
    page = html.unescape((await admin.get("/admin")).text)
    block = section(page, "attention")
    # Two lists: overdue (a decision is needed, a strike was given) and still open in time.
    overdue, still_open = block.split("Overdue")[1].split("Still open")
    for pd in (late.first, late.second):
        assert excuse(pd) in overdue and excuse(pd) not in still_open
    assert "strike given" in overdue and "Their strike for it is taken back" in overdue
    for pd in [now.first, now.second, *trash.potential_drivers]:
        assert excuse(pd) in still_open
        assert f"/potential-drivers/{pd.id}/drove?next=/admin" in still_open
    assert "No strike, no credit" in still_open
    # Whoever is one strike before a penalty week.
    near = words(block.split("Close to a penalty week")[1])
    assert f"Person {people[3].room_number} 2 of {settings.strike_threshold} strikes" in near
    # The daily job says what it does before it runs.
    assert 'hx-confirm="Run the daily job now? It marks overdue duties missed' in page

    def attention_count(page) -> int:
        return int(re.search(r"Needs attention (\d+)", words(page)).group(1))

    # Keep missed: only in the list that asks for a decision.
    keep = f"/admin/potential-drivers/{late.first.id}/keep-missed"
    assert keep in overdue and keep not in section(page, "duties")
    count = attention_count(page)
    r = await admin.post(keep)
    assert r.headers["location"] == "/admin"
    page = html.unescape((await admin.get("/admin")).text)
    assert "Kept as missed, the strike stays" in page and attention_count(page) == count - 1
    block = section(page, "attention")
    assert f"/admin/potential-drivers/{late.first.id}/" not in block
    assert f"/admin/potential-drivers/{late.second.id}/keep-missed" in block
    # Still in the duties table, marked as decided, where it can be marked done or excused
    # after all.
    table = rows(section(page, "duties"))
    [row] = [row for row in table if excuse(late.first) in row]
    [other_row] = [row for row in table if excuse(late.second) in row]
    assert "kept missed" in words(row) and "kept missed" not in words(other_row)
    # Nothing else changes: still missed, and the strike for it stays.
    pd = await session.get(PotentialDriver, late.first.id)
    await session.refresh(pd)
    assert pd.status is Status.missed and pd.kept_missed_at is not None
    assert [s.revoked_at for s in await strikes.for_potential_driver(session, pd.id)] == [None]


async def test_admin_manages_residents(session, browser):
    people = await make_users(session, n=3)
    admin = await browser("p1")
    new = {"name": "New Person", "room_number": "E120", "username": "new", "password": "pw123456"}
    await admin.post("/admin/users", data=new)
    assert "New E120" in words(section(await admin.get("/admin"), "residents"))

    # Too long: refused with a message, nothing created or changed.
    r = await admin.post("/admin/users", data=new | {"name": "x" * 200, "username": "longname"})
    assert "at most" in await follow(admin, r)
    data = {"name": "x" * 200, "room_number": people[1].room_number}
    assert "at most" in await follow(
        admin, await admin.post(f"/admin/users/{people[1].id}/edit", data=data)
    )
    assert await session.scalar(select(User).where(User.username == "longname")) is None
    assert await session.scalar(select(User.name).where(User.id == people[1].id)) == "Person 2"

    # A duty that's gone (e.g. on an old page) is a message, not an error page.
    r = await admin.post("/admin/potential-drivers/999999/excuse")
    assert r.headers["location"] == "/admin" and "Duty not found" in await follow(admin, r)
    assert f'hx-confirm="Pause {people[2].display_name}?' in html.unescape(
        (await admin.get("/admin")).text
    )
    r = await admin.post(f"/admin/users/{people[0].id}/delete")
    assert "You can't delete yourself" in await follow(admin, r)


async def test_admin_absences(session, browser, clock):
    people = await make_users(session, n=4)
    resident = people[2]
    later = clock.today + timedelta(days=5)
    end = later + timedelta(days=2)
    form = {
        "user_id": str(resident.id),
        "start_date": later.isoformat(),
        "end_date": end.isoformat(),
    }
    admin = await browser("p1")
    r = await admin.post("/admin/aways", data=form)
    assert r.headers["location"] == "/admin"
    page = await follow(admin, r)
    assert f"Away period added for {resident.display_name}" in page
    assert f"{short_date(later)}–{long_date(end)}" in page
    # A wrong period comes back as a message.
    r = await admin.post("/admin/aways", data=form | {"end_date": clock.today.isoformat()})
    assert "The end can't be before the start" in await follow(admin, r)

    away_id = await session.scalar(select(AwayPeriod.id))
    r = await admin.post(f"/me/away/{away_id}/delete?next=/admin")
    assert r.headers["location"] == "/admin"
    assert await session.scalar(select(func.count(AwayPeriod.id))) == 0


async def test_admin_strike_and_duty_lists(session, browser, clock):
    people = await make_users(session, n=4)
    admin = await browser("p1")
    # Empty lists are a plain sentence, not a table with only a header.
    page = await admin.get("/admin")
    for part, sentence in [
        ("absences", "Nobody is away."),
        ("dishwasher", "Nobody has emptied it this month yet."),
        ("strikes", "No active strikes."),
    ]:
        assert sentence in words(section(page, part)) and "<table" not in section(page, part)

    old_day = clock.today - timedelta(days=10)
    await kitchen_pair(session, people, old_day)
    pair = await kitchen_pair(session, people, clock.today)
    await give_strikes(session, people[2].id, today=clock.today)  # used up by a penalty week
    await strikes.issue(session, user_id=people[3].id, reason="still on", today=clock.today)
    revoked = await strikes.issue(
        session, user_id=people[1].id, reason="taken back", today=clock.today
    )
    await strikes.revoke(session, revoked, clock.now)
    await session.commit()
    page = html.unescape((await admin.get("/admin")).text)

    # Revoked and used strikes are folded away. A used one says so, and revoking it says
    # what happens to its penalty week; an active one doesn't.
    current, folded = section(page, "strikes").split('<details class="older">')
    assert "still on" in current and "taken back" not in current and "r0" not in current
    assert "taken back" in folded
    [used] = [row for row in rows(folded) if "r0" in row]
    assert "penalty week" in words(used)
    assert "that week is cancelled and the other strikes count again" in used
    [active] = [row for row in rows(current) if "still on" in row]
    assert "Revoke" in words(active) and "cancelled" not in active
    assert f"hx-confirm=\"Revoke {people[3].display_name}'s strike from" in page

    # The last 7 days of duties in view, older ones folded (with all their buttons).
    kitchen = section(page, "duties").split("<h3>Trash duty</h3>")[0]
    recent, earlier = kitchen.split('<details class="older">')
    day = f"{old_day:%a} {short_date(old_day)}"
    assert excuse(pair.first) in recent and day not in recent
    assert day in earlier and "1 earlier day" in words(earlier) and "/excuse" in earlier

    # The confirmations name the person.
    name = pair.p1.display_name
    assert f"hx-confirm=\"Excuse {name}'s kitchen duty on" in page
    await admin.post(f"/potential-drivers/{pair.first.id}/drove?next=/admin")
    page = html.unescape((await admin.get("/admin")).text)
    assert f"hx-confirm=\"Dispute {name}'s kitchen duty on" in page
