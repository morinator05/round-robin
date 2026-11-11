# Running Round Robin on a server

The server runs the released Docker image (amd64 or arm64, e.g. a Raspberry Pi with a 64-bit
OS) behind your own reverse proxy, which handles TLS. Every night at about 04:00 it checks
for a new release. If there is one, it backs up the database, switches to the new version
and runs its migrations. Nobody has to log in to the machine for updates.

## Setup

Requirements: Docker Engine 25 or newer with the Compose plugin
(<https://docs.docker.com/engine/install/debian/>, also for Raspberry Pi OS), plus git.

```bash
sudo git clone https://github.com/morinator05/round-robin.git /opt/round-robin
cd /opt/round-robin
sudo cp .env.example .env && sudo chmod 600 .env
sudo nano .env            # see below
sudo deploy/update.sh     # first start: pulls the newest release and starts it
```

In `.env`, set at least:

- `POSTGRES_PASSWORD`: random, `openssl rand -hex 16`. Choose it before the first start;
  the database keeps the password it was created with.
- `SECRET_KEY`: random, `openssl rand -hex 32`.
- `SECURE_COOKIES=true`, since the proxy serves HTTPS.
- `FLOOR_NAME` and the `ADMIN_*` variables for the first floor speaker. You can also create
  them later with `sudo docker compose exec app python -m scripts.create_admin`.

Then turn on the nightly updates:

```bash
sudo cp deploy/round-robin-update.service deploy/round-robin-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now round-robin-update.timer
systemctl list-timers round-robin-update.timer    # shows the next run
```

The units expect the repository in `/opt/round-robin`. For another place, change
`WorkingDirectory` and `ExecStart` in the `.service` file.

## Settings

All in `.env`; `.env.example` has them with comments.

| Setting | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | – | Required, ≥ 32 chars (`openssl rand -hex 32`). Signs the session cookie. |
| `POSTGRES_PASSWORD` | `e1` | Database password. Use a random one on a server, set before the first start (later: `ALTER USER <user> PASSWORD '...'` in the database). |
| `DATABASE_URL` | local database | Only needed outside Docker. |
| `TIMEZONE` | `Europe/Berlin` | Time zone for dates and deadlines. |
| `FLOOR_NAME` | – | Optional floor name in the header ("Round Robin E1"). |
| `DEADLINE_HOUR` | `8` | Deadline hour on the day after a duty; the next people are chosen right after it. |
| `KITCHEN_PEOPLE_PER_DAY` | `2` | People per kitchen duty. |
| `TRASH_PEOPLE_PER_WEEK` | `1` | People per trash duty. |
| `STRIKE_THRESHOLD` | `3` | Strikes per penalty week. |
| `DISHWASHER_COOLDOWN_MINUTES` | `90` | Minutes after an emptying before the next one can be marked. |
| `DISHWASHER_HEROES` | `2` | How many of a month's top emptiers get the next month off kitchen duty. |
| `PREVIEW_DAYS` | `14` | Days shown in the plan. |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_NAME`, `ADMIN_ROOM` | – | Optional first floor speaker, created on startup only while there are no users (so a deleted admin doesn't come back). |
| `SECURE_COOKIES` | `false` | Send the login cookie only over HTTPS. Turn on behind TLS. |
| `APP_IMAGE` | newest release | Server only: stay on one version, e.g. `ghcr.io/morinator05/round-robin:1.2.0`. |
| `APP_BIND` | `127.0.0.1` | Where the app's port 8000 is reachable. Local only, for the reverse proxy. |

## Reverse proxy

The app listens on `127.0.0.1:8000` only. The proxy forwards to it and passes on the
client's address, which the login limits rely on.

Caddy (gets the certificate by itself):

```
duties.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

nginx (inside your `server { … }` with TLS):

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

If the proxy runs in a Docker container itself, connect it to the `round-robin_default`
network and forward to `app:8000`. Don't publish the app on other addresses with
`APP_BIND`: anyone who can reach port 8000 directly can fake their address.

## Releasing a version

```bash
gh release create v1.3.0 --generate-notes
```

You can also use the GitHub web page (Releases → Draft a new release) with a new tag like
`v1.3.0`. CI tests the tagged commit, then publishes `ghcr.io/morinator05/round-robin` as
`1.3.0`, `1.3` and `latest`. Servers pick up `latest` the next night. The admin page shows
the running version at the bottom.

Use `vMAJOR.MINOR.PATCH`: patch for fixes, minor for new features. Pre-releases such as
`v1.3.0-rc.1` are published under their own tag only, never as `latest`, so you can try one
with `APP_IMAGE` (see Settings) without updating the servers.

After the very first release, check on GitHub (your profile → Packages → round-robin →
Package settings) that the package is **public**, so servers can pull it without logging in.

## What happened?

- `journalctl -u round-robin-update`: every nightly run (up to date, backup, updated to vX,
  or the error).
- `systemctl status round-robin-update`: shows *failed* if the last update didn't come up
  healthy.
- `sudo docker compose logs --tail 100 app`: the app itself.
- To update right away instead of at night: `sudo systemctl start round-robin-update`.

## Backups and going back

Before every update, `deploy/update.sh` writes a dump to `backups/` and keeps the newest 10.
Copy them off the machine now and then; they don't survive a dead SD card.

To go back to an older version, e.g. after a bad release:

1. Set `APP_IMAGE=ghcr.io/morinator05/round-robin:1.2.0` in `.env`. This also stops the
   automatic updates until you remove the line.
2. If the newer version changed the database, first restore the dump taken before it:

   ```bash
   sudo docker compose stop app
   sudo docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
   gunzip -c backups/<file>.sql.gz | sudo docker compose exec -T db sh -c 'psql -q -U "$POSTGRES_USER" "$POSTGRES_DB"'
   ```

3. Run `sudo deploy/update.sh`.

To return to automatic updates, remove `APP_IMAGE` from `.env` and run `sudo deploy/update.sh`.
