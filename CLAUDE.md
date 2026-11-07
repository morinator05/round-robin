# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Round Robin assigns kitchen duty (daily, 2 people, one of them cleans) and trash duty (weekly,
1 person) on a dorm floor fairly, tracks who did it and gives strikes for missed duties. The
app's rules (fairness, deadlines, strikes, absences, admin powers) are in `README.md`; that
is the spec for behaviour. The UI text is English.

## Commands

```bash
cp .env.example .env                        # SECRET_KEY must be ≥ 32 chars, not "change-me…"
docker compose up -d db                     # Postgres 16 on localhost:5432
uv sync
uv run alembic upgrade head
uv run fastapi dev app/main.py              # dev server with reload, http://localhost:8000
uv run pytest                               # DB tests are skipped if Postgres is down
uv run pytest tests/test_rules.py::test_tick_assigns_kitchen_and_trash   # one test
uv run ruff check . && uv run ruff format --check .   # lint as in CI
uv run alembic revision --autogenerate -m "..."      # migration after changing models.py
uv run python scripts/seed.py [--days 30]   # test residents (+ a simulated month on an empty DB)
uv run python scripts/create_admin.py       # first floor speaker (admin) account
```

CI (`.github/workflows/ci.yml`) runs ruff and pytest against Postgres 16. With `CI` set, an
unreachable database is an error instead of a skip. The Docker image runs
`alembic upgrade head` on every start.

## Architecture

FastAPI with server-rendered Jinja templates and htmx (vendored, no JS build; the Overpass
font is self-hosted in `app/static/fonts`, so pages make no outside requests), async
SQLAlchemy on Postgres (asyncpg), APScheduler.

**Fairness is computed, never stored.** `app/services/carpool.py` is pure (no DB) and
implements Fagin & Williams' fair carpool algorithm (1983) plus our extensions (listed in
its module docstring); the code uses the paper's vocabulary. Each duty is its own carpool:
a **trip** is one kitchen day / one trash week, its **riders** are the members available
for it (active, not away), and each rider is charged a fair share of `1/len(riders)`
(`charge_riders`). Each trip gets `n` **potential drivers** (kitchen 2, trash 1); whoever
marks it done is the **driver** and gets one drive (`credit_driver`). Score = drives −
fair share (the paper's score). The stats page shows it as whole points, 100 per duty,
via `carpool.display_points` (largest-remainder rounding, so they still sum to 0).
`choose_potential_drivers` takes the lowest `effective_score` (score plus `pending_share`
of undriven trips they're already a potential driver on), ties broken by `last_drove`, then
room. All values are `Fraction`s.
`app/services/duties.load_balances` rebuilds every balance from the full trip history on
each call, and only driven trips count (`duties.was_driven`), so scores sum to 0. The one
deliberate exception: a moved-out resident's score leaves with them.

**Trips.** A `Trip` has `Rider` rows (who was available, so shares stay correct later) and
`PotentialDriver` rows (who was chosen, with a `Status`: `pending`, `drove`, `rode` when
someone else drove, `missed`, `excused`). Only the current day's kitchen trip and week's
trash trip are created in the DB (`duties.materialize`). Future trips exist only as a
simulation in `duties.preview` → `carpool.plan`, so they always reflect current balances
and absences. While nobody has driven the latest trip (the **open trip**, normally today's),
`duties.preview` returns one plan per pending potential driver (`Preview.plans`), each
simulated as if that one drove it (`carpool.assume_driven`: riders charged, reserved
shares released, drive credited). The dashboard shows one of them, chosen with `?cleaner=`.
Older undriven trips keep their reserved shares, as when real trips are chosen.

**Penalty weeks.** `strikes.issue` calls `check_threshold`; at `STRIKE_THRESHOLD` active
strikes it creates a trash `Trip` with `is_penalty=True` in the next free week and marks
those strikes as redeemed by it (`Strike.redeemed_by_trip_id`). Penalty trips don't count
for fairness.

**Dishwasher heroes.** `app/services/dishwasher.py`: residents mark `DishwasherClear`s (one
per `DISHWASHER_COOLDOWN_MINUTES` for everyone, under an advisory lock). Heroes are computed,
never stored: `dishwasher.heroes(today)` is the top `DISHWASHER_HEROES` of the previous
calendar month (ties: earlier last emptying). `duties.load_members(session, duty, today)` adds
today's month as an away period of each hero for kitchen only, so they neither ride nor are
chosen and owe no share; `reconcile` therefore loads members per duty, and `preview` takes
`today` (next month's heroes aren't known yet). Only the current month's entries can be
deleted, so a hero month never changes once it runs.

**Moved-out users.** Deleting a user sets `user_id` to NULL on their riders/potential
drivers (`ondelete="SET NULL"`) so history stays. `charge_riders` takes `None` ids: they
still count for the share size but their share is dropped, not passed on to others.

**The daily job.** `jobs.daily_tick` takes a Postgres advisory lock, marks overdue
potential drivers missed and issues strikes (`duties.process_overdue`), then materializes
today's trips, and commits. A day's (on Monday the week's) trip is only materialized from
`duties.choose_at` on: the deadline hour of its first day, right after the previous trip's
deadline, so only one trip is ever open (the preview's per-driver plans rely on that).
Until then `duties.current_trip` (the dashboard card) is the previous trip. It runs on
startup, at `DEADLINE_HOUR`:05 (scheduler in `app/main.py`), from the admin page, and from
the dashboard when the current trips are missing, so it must stay idempotent. Absences, pausing and deleting users call
`duties.reconcile`, which removes them as riders and pending potential drivers of
current/upcoming trips and chooses replacements.

**Layers.**
- `app/services/*`: all rules. Services `flush` but don't `commit` (except `daily_tick`).
  They raise `DutyError` / `UserError` with an English, user-facing message.
- `app/web.py`: all routes. They parse input, call a service, commit, and turn the
  service errors into a session flash. Every POST answers with a 303 redirect. htmx
  requests are detected via the `HX-Request` header. New routes go on `router`, which
  requires a login for every route; only login/logout are on `public`. Nothing with
  personal data may be visible without a login (`test_every_page_needs_a_login`).
- `app/deps.py`: `Session`, `OptionalUser`, `CurrentUser`, `AdminUser` are `Annotated`
  route parameter types; `CurrentUser` raises `LoginRequired`, which `main.py` turns into a
  redirect to `/login`. A session counts only if it carries the user's current
  `User.session_token`; `auth.end_sessions` (logout, password changes) sets a new one and so
  logs the user out everywhere. Log in with `web.start_session`.
- `app/formatting.py`: how dates are written (`short_date` "3 Nov", `long_date`
  "3 Nov 2026"), for templates and strike reasons.
- `app/config.py`: settings from `.env`. Use `settings.now()` / `settings.today()` (in the
  configured time zone), never `date.today()`. Services take `today` / `now` as arguments,
  which is how tests set the time.

## Tests

- `tests/conftest.py` points at a separate `e1_test` database (created if missing), drops
  the `public` schema and recreates all tables per test from `Base.metadata` (not via
  migrations), disables the scheduler, and monkeypatches `app.db.SessionLocal`. Use the
  `session` fixture.
- `tests/test_carpool.py`: the pure fairness logic. `tests/test_rules.py`: rules against
  the DB, using `make_users` and `at(day, hour)` with a fixed `MONDAY`. Web pages are
  tested through `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))`.
- pytest runs with `asyncio_mode = "auto"`, so async tests need no marker.

## Conventions

- Schema changes need an Alembic migration in `migrations/versions/` (ruff ignores that
  folder).
- Ruff: line length 100, rules E/F/W/I/UP/B. `UP042` is ignored on purpose: enums stay
  `str, Enum` because `StrEnum` changes how values print in templates.
- `README.md` is short and written for residents/admins; keep implementation details out.
