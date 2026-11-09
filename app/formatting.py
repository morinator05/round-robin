"""How dates, times and numbers are written in the UI and in messages ("3 Nov", "14:32").

`%a` and `%b` give English names because the app never changes the C locale.
"""

from datetime import date, datetime

from app.config import settings


def short_date(d: date | None) -> str:
    """Day and month: "3 Nov"."""
    return f"{d.day} {d:%b}" if d else ""


def long_date(d: date | None) -> str:
    """Day, month and year: "3 Nov 2026"."""
    return f"{d.day} {d:%b %Y}" if d else ""


def weekday(d: date) -> str:
    """Short weekday name: "Tue"."""
    return f"{d:%a}"


def month_name(d: date) -> str:
    """Month name: "November"."""
    return f"{d:%B}"


def clock(moment: datetime) -> str:
    """The time of day like "14:32", in the configured time zone."""
    return f"{moment.astimezone(settings.tz):%H:%M}"


def local_date(moment: datetime) -> date:
    """The date of `moment` in the configured time zone."""
    return moment.astimezone(settings.tz).date()


def date_time(moment: datetime | None) -> str:
    """Date and time, "5 Nov 2026 21:30"; in a narrow column only the time wraps."""
    if moment is None:
        return ""
    local = moment.astimezone(settings.tz)
    return f"{long_date(local)} {local:%H:%M}".replace(" ", "\u00a0", 2)


def signed(points: int) -> str:
    """Points with their sign: "+37", "−112" (a real minus sign), "0"."""
    return f"{points:+d}".replace("-", "\u2212") if points else "0"


def times(n: int) -> str:
    """How often: "once", "3 times"."""
    return "once" if n == 1 else f"{n} times"


def trip_dates(trip) -> str:
    """A trip's dates: "Mon 2 Nov", a week "Mon 2 Nov–8 Nov"."""
    first = f"{weekday(trip.start_date)} {short_date(trip.start_date)}"
    return first if trip.end_date == trip.start_date else f"{first}–{short_date(trip.end_date)}"
