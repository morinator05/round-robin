"""Take the screenshots shown in the README (docs/screenshots), in light and dark.

Use a separate demo database, not real data:

    docker compose exec db createdb -U a4 a4_screens
    export DATABASE_URL=postgresql+asyncpg://a4:a4@localhost:5432/a4_screens
    uv run alembic upgrade head
    uv run python scripts/seed.py --days 30 --random-seed 4
    docker compose exec db psql -U a4 -d a4_screens \\
        -c "UPDATE users SET is_admin = true WHERE username = 'anna'"
    uv run fastapi run app/main.py --port 8001 &

    uv run --with playwright playwright install chromium     # once
    uv run --with playwright python scripts/screenshots.py

Afterwards: stop the app and `docker compose exec db dropdb -U a4 a4_screens`.
The PNGs are compressed with pngquant if it is installed.
"""

import argparse
import shutil
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

# name: (path, whole page); the long pages are cut at the window height
PAGES = {
    "overview": ("/", True),
    "statistics": ("/stats", True),
    "history": ("/history", False),
    "profile": ("/me", True),
    "admin": ("/admin", False),
}
DESKTOP = {"width": 1280, "height": 900}
MOBILE = {"width": 390, "height": 844}


def main(url: str, username: str, password: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    shots: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("light", "dark"):
            for name, viewport, pages in (
                ("", DESKTOP, PAGES),
                ("-mobile", MOBILE, {"overview": ("/", True)}),
            ):
                context = browser.new_context(
                    viewport=viewport,
                    device_scale_factor=2,
                    color_scheme=scheme,
                    locale="en-GB",
                    timezone_id="Europe/Berlin",
                )
                page = context.new_page()
                page.goto(f"{url}/login")
                page.fill("input[name=username]", username)
                page.fill("input[name=password]", password)
                page.click("button:has-text('Log in')")
                page.wait_for_load_state("networkidle")
                for page_name, (path, full_page) in pages.items():
                    page.goto(url + path)
                    page.wait_for_load_state("networkidle")
                    file = out / f"{page_name}{name}-{scheme}.png"
                    page.screenshot(path=file, full_page=full_page)
                    shots.append(file)
                context.close()
        browser.close()

    if shutil.which("pngquant"):
        subprocess.run(
            ["pngquant", "--force", "--skip-if-larger", "--ext", ".png", *map(str, shots)],
            check=False,
        )
    for file in shots:
        print(f"{file} ({file.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8001")
    parser.add_argument("--user", default="anna")
    parser.add_argument("--password", default="passwort123")
    parser.add_argument("--out", type=Path, default=Path("docs/screenshots"))
    args = parser.parse_args()
    main(args.url.rstrip("/"), args.user, args.password, args.out)
