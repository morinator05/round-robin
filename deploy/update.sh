#!/usr/bin/env bash
# Updates the app to the newest release, if there is one: pulls the image, checks out the
# matching release of this repository (compose file, this script), backs up the database
# and restarts. Run nightly by round-robin-update.timer; safe to run by hand any time.
set -euo pipefail
cd "$(dirname "$0")/.."

image=$(docker compose config app | awk '/^  app:/ { app = 1 } app && /^    image:/ { print $2; exit }')
docker compose pull --quiet app

container=$(docker compose ps -q app)
running=$([ -n "$container" ] && docker inspect -f '{{.Image}}' "$container" || true)
pulled=$(docker image inspect -f '{{.Id}}' "$image")
if [ "$running" = "$pulled" ]; then
  echo "Up to date ($image)."
  exit 0
fi

# The release the image was built from, e.g. 1.3.0 (empty for a locally built image).
version=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "$image")
if [ -n "$version" ]; then
  git fetch --quiet --tags origin
  git -c advice.detachedHead=false checkout --quiet "v$version"
fi

# A dump before every update: migrations run on start and can't always be undone.
if [ -n "$(docker compose ps -q db)" ]; then
  mkdir -p backups
  file="backups/$(date +%F-%H%M)-${version:-unknown}.sql.gz"
  docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$file.part"
  mv "$file.part" "$file"
  # Keep the newest 10.
  ls -1t backups/*.sql.gz | tail -n +11 | xargs -r rm --
  echo "Backed up the database to $file."
fi

# --wait: fails (and so does the systemd unit) if the new version doesn't become healthy.
docker compose up -d --no-build --wait --wait-timeout 300
docker image prune -f > /dev/null
echo "Updated to ${version:+v$version }($image)."
