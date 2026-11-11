# Development

How to run Round Robin locally, test it and find your way around the code. The app's rules
are in the [README](../README.md); running it on a server is in
[deployment.md](deployment.md).

## Tech stack

- **Backend:** Python 3.12, FastAPI, APScheduler for the daily job
- **Database:** PostgreSQL 16, SQLAlchemy (async, asyncpg), Alembic migrations
- **Frontend:** server-rendered Jinja templates, htmx for page loads without a full reload
  and in-place updates, plain CSS. No JavaScript build step.
- **Auth:** signed session cookie (itsdangerous), passwords hashed with Argon2 (pwdlib)
- **Config:** pydantic-settings, read from `.env` (all settings: [deployment.md](deployment.md#settings))
- **Tooling:** uv, pytest, ruff, GitHub Actions CI
- **Deployment:** Docker Compose (app + database), image on GHCR for amd64 and arm64

## Run it locally

With auto-reload (database in Docker, app local):

```bash
cp .env.example .env                    # set SECRET_KEY: openssl rand -hex 32
docker compose up -d db
uv sync
uv run alembic upgrade head
uv run fastapi dev app/main.py          # http://localhost:8000
uv run python -m scripts.create_admin   # first floor speaker
```

Or everything in Docker, built from your checkout:

```bash
docker compose up -d --build            # database + app, http://localhost:8000
docker compose exec app python -m scripts.create_admin
```

- **Test residents:** `uv run python -m scripts.seed` (or `docker compose exec app python -m
  scripts.seed`). Add `--days 30` (on an empty database) for a simulated month of duties,
  misses and strikes.
- **Migrations:** after changing `app/models.py`,
  `uv run alembic revision --autogenerate -m "..."`. They run on every start of the image.
- **Reset:** `docker compose down -v` deletes the database.

## Tests

```bash
uv run pytest   # needs the database from `docker compose up -d db`
uv run ruff check . && uv run ruff format --check .   # lint, as in CI
```

CI also builds the Docker image on amd64 and arm64 and starts it against an empty database.
Releases are described in [deployment.md](deployment.md#releasing-a-version).

## Code

- `app/services/carpool.py`: the fairness logic, without database (start reading here)
- `app/services/`: the other rules (duties, strikes, dishwasher, users, login, daily job)
- `app/web/`: pages and form actions, one module per page; templates in `app/templates/`
- `app/models.py`: database tables; migrations in `migrations/`
- `scripts/`: create the first floor speaker, add test residents
- `deploy/`: the nightly update script and its systemd units
