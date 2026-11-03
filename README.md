# Round Robin A4

Web app for the cleaning duties on dorm floor A4: assigns kitchen and trash duty fairly,
tracks who did it and gives strikes for missed duties. The UI is in German.

## Rules

**Duties**
- Küchendienst (kitchen): daily, 2 people, one of them cleans.
- Mülldienst (trash): weekly (Mon–Sun), 1 person.
- Whoever cleaned clicks "Erledigt" (only on their own duty, from its first day until the
  deadline). The other person becomes "nicht nötig".
- Deadline: 12:00 the day after the duty ends. After that the duty is "verpasst" (missed).
- Statuses: offen, erledigt, verpasst, nicht nötig, entschuldigt (excused), beanstandet (disputed).

**Fairness**

Each slot is one job, split equally among everyone available (active, not away). Debt
("Schuld") = shares − jobs done. The highest debt is picked next; ties go to whoever cleaned
longest ago (never cleaned = first). Being put on a duty without cleaning doesn't move you
back. Example with 15 residents: a slot adds 1/15 ≈ 0.07 to everyone, the person who cleans
ends at 0.07 − 1 ≈ −0.93. All debts sum to 0.
- Away or paused: no share for that time.
- New residents start at 0.
- Only the person who cleaned gets credit. Missed duties give no credit.
- Kitchen and trash are counted separately; penalty weeks not at all.

**Strikes**
- Given for a missed duty (everyone on it), a disputed "Erledigt", or by hand.
- Marking a missed duty done or excusing it later revokes its strikes.
- 3 strikes → Straf-Mülldienst: the next trash week without an assignment in which the person
  isn't away. It replaces the regular trash duty, doesn't count for fairness and uses up the
  3 strikes.

**Absences** ("Abwesenheit", on "Mein Profil"): not picked during that time; open duties in
it are reassigned immediately. Deleting the absence doesn't give them back.

**Flursprecher (admin)**, on "Verwaltung"
- Add, edit, pause ("Deaktivieren") and delete ("Löschen (Auszug)") residents; hand over the
  Flursprecher role.
- Mark any duty done (also after the deadline), excuse it ("Entschuldigen": no strike, no
  credit) or dispute it ("Beanstanden", also on "Verlauf": the claimer loses the credit and
  gets a strike).
- Give and revoke strikes; run the daily job by hand.
- Move-out/in: delete the old resident and add the new one as a new account (don't rename).
  The history stays as "ehemalig"; nobody inherits the share.
- Room numbers like `A403`: building, floor, room 01–99.

**Daily job** (00:05 and 12:05): marks missed duties and gives the strikes, then assigns
today's kitchen and this week's trash duty. Residents who become unavailable (away, paused,
deleted) are replaced on their open duties immediately.

## Pages

| Page | Access | Content |
|---|---|---|
| Übersicht | public | Today's duties, plan for the next 14 days. Today's kitchen pair is shown as "Erledigt" / "Ausgelassen": where they land if they clean or don't |
| Statistik | public | Per resident: done, missed, debt, strikes |
| Verlauf | login | Duty history, 4 weeks per page |
| Mein Profil | login | Absences, strikes, recent duties, password |
| Verwaltung | Flursprecher | Admin functions |

## Settings

In `.env`, see `.env.example`.

| Setting | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | – | Required, ≥ 32 chars (`openssl rand -hex 32`). Signs the session cookie. |
| `POSTGRES_PASSWORD` | `a4` | Database password. Use a random one on a server. |
| `DATABASE_URL` | local database | Only needed outside Docker. |
| `TIMEZONE` | `Europe/Berlin` | Time zone for dates and deadlines. |
| `DEADLINE_HOUR` | `12` | Deadline hour … |
| `DEADLINE_DAYS_AFTER` | `1` | … this many days after the duty ends. |
| `KITCHEN_PEOPLE_PER_DAY` | `2` | People per kitchen duty. |
| `TRASH_PEOPLE_PER_WEEK` | `1` | People per trash duty. |
| `STRIKE_THRESHOLD` | `3` | Strikes per penalty week. |
| `PREVIEW_DAYS` | `14` | Days shown in the plan. |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_NAME`, `ADMIN_ROOM` | – | Optional first Flursprecher, see below. |

## Tech stack

- **Backend:** Python 3.12, FastAPI, APScheduler for the daily job
- **Database:** PostgreSQL 16, SQLAlchemy (async, asyncpg), Alembic migrations
- **Frontend:** server-rendered Jinja templates, htmx for page loads without a full reload
  and in-place updates, plain CSS. No JavaScript build step.
- **Auth:** signed session cookie (itsdangerous), passwords hashed with Argon2 (pwdlib)
- **Config:** pydantic-settings, read from `.env`
- **Tooling:** uv, pytest, ruff, GitHub Actions CI
- **Deployment:** Docker Compose (app + database)

## Setup

```bash
cp .env.example .env                    # set SECRET_KEY: openssl rand -hex 32
docker compose up -d --build            # database + app, http://localhost:8000
docker compose exec app uv run --no-dev python scripts/create_admin.py   # first Flursprecher
```

- **First Flursprecher without the script:** set the `ADMIN_*` variables in `.env`. The app
  creates the account on startup only while there are no users, so a deleted admin doesn't
  come back.
- **Server:** set a random `POSTGRES_PASSWORD` before the first start (later changes need
  `ALTER USER a4 PASSWORD '...'` in the database).
- **Update:** `docker compose up -d --build`. Migrations run on every start.
- **Test residents:** `docker compose exec app uv run --no-dev python scripts/seed.py`. Add
  `--days 30` (on an empty database) for a simulated month of duties, misses and strikes.
- **Reset:** `docker compose down -v` deletes the database.

Development with auto-reload (database in Docker, app local):

```bash
docker compose up -d db
uv sync
uv run alembic upgrade head
uv run fastapi dev app/main.py          # http://localhost:8000
```

## Tests

```bash
uv run pytest   # needs the database from `docker compose up -d db`
uv run ruff check . && uv run ruff format --check .   # lint, as in CI
```

## Code

- `app/services/scheduling.py`: the fairness logic, without database (start reading here)
- `app/services/`: the other rules (duties, strikes, users, login, daily job)
- `app/web.py`: pages and form actions; templates in `app/templates/`
- `app/models.py`: database tables; migrations in `migrations/`
- `scripts/`: create the first Flursprecher, add test residents
