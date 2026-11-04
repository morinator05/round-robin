"""App settings, read from environment variables or `.env` (names are case-insensitive)."""

from datetime import date, datetime
from functools import cached_property
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings with their defaults. `DEADLINE_HOUR=10` in the environment overrides
    `deadline_hour`, and so on."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://a4:a4@localhost:5432/a4"
    # Signs the login cookie: whoever knows it can log in as anyone. No usable default on purpose.
    secret_key: str = ""
    timezone: str = "Europe/Berlin"
    # Shown in the header and page title: "Round Robin <floor_name>".
    floor_name: str = "A4"
    # A duty must be marked done by this hour, `deadline_days_after` days after it ended.
    # Each day's (on Monday also the week's) potential drivers are chosen at this hour too,
    # right after the previous duty's deadline, so only one duty is ever open at a time.
    deadline_hour: int = 8
    deadline_days_after: int = 1
    # Potential drivers chosen for each trip (day / week); only one of them has to drive.
    kitchen_people_per_day: int = 2
    trash_people_per_week: int = 1
    # Active strikes that turn into a penalty trash week.
    strike_threshold: int = 3
    # How far ahead the dashboard preview looks.
    preview_days: int = 14
    # Off in tests, so no background job touches the test database.
    run_scheduler: bool = True
    # Optional first floor speaker, created on startup while there are no users at all.
    admin_username: str = ""
    admin_password: str = ""
    admin_name: str = ""
    admin_room: str = ""

    @field_validator("secret_key")
    @classmethod
    def secret_key_must_be_random(cls, value: str) -> str:
        """Refuses to start with an empty, placeholder or short key."""
        if len(value) < 32 or value.startswith("change-me"):
            raise ValueError(
                "SECRET_KEY must be a random string of at least 32 characters, "
                "e.g. from `openssl rand -hex 32`"
            )
        return value

    @cached_property
    def tz(self) -> ZoneInfo:
        """The configured time zone; all dates and deadlines are in this zone."""
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        """The current time in the configured time zone."""
        return datetime.now(self.tz)

    def today(self) -> date:
        """Today's date in the configured time zone (not the server's)."""
        return self.now().date()


settings = Settings()
