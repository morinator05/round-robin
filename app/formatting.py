"""How dates are written in the UI and in strike reasons ("3 Nov", "3 Nov 2026").

`%a` and `%b` give English names because the app never changes the C locale.
"""

from datetime import date


def short_date(d: date | None) -> str:
    """Day and month: "3 Nov"."""
    return f"{d.day} {d:%b}" if d else ""


def long_date(d: date | None) -> str:
    """Day, month and year: "3 Nov 2026"."""
    return f"{d.day} {d:%b %Y}" if d else ""
