"""All pages and form actions: server-rendered Jinja templates, htmx for small updates.

Routes only parse input, call a service (through `common.act`) and render or redirect; the
rules live in `app/services/`. Every form POST answers with a redirect (303), so reloading
the page doesn't submit the form again.
"""

from app.web import admin, dashboard, login, pages, profile  # noqa: F401  (adds the routes)
from app.web.common import person_html, public, router, safe_next

__all__ = ["person_html", "public", "router", "safe_next"]
