"""App settings, read from environment variables or `.env` (names are case-insensitive)."""

import re
from datetime import date, datetime
from functools import cached_property
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings with their defaults."""

    # hide_input_in_errors: an invalid setting's error names it but doesn't show its value,
    # so a mistyped secret (key, bot token) doesn't end up in the log.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = "postgresql+asyncpg://e1:e1@localhost:5432/e1"
    # Signs the login cookie: whoever knows it can log in as anyone. No usable default on purpose.
    secret_key: str = ""
    timezone: str = "Europe/Berlin"
    # Optional, shown in the header and page title: "Round Robin <floor_name>".
    floor_name: str = ""
    # A duty must be marked done by this hour on the day after it ended. Each day's (on
    # Monday also the week's) potential drivers are chosen at this hour too, right after the
    # previous duty's deadline, so only one duty is ever open at a time.
    deadline_hour: int = 8
    # Potential drivers chosen for each trip (day / week); only one of them has to drive.
    kitchen_people_per_day: int = 2
    trash_people_per_week: int = 1
    # Active strikes that turn into a penalty trash week.
    strike_threshold: int = 3
    # Minutes in which whoever marked a duty done or the dishwasher emptied can take it back.
    undo_minutes: int = 10
    # How far ahead the dashboard preview looks.
    preview_days: int = 14
    # This many dishwasher entries (by anyone) less than this many minutes apart, first to
    # last, are suspicious: the floor speaker checks them on the admin page.
    dishwasher_suspicious_count: int = 3
    dishwasher_suspicious_minutes: int = 90
    # How many of a month's top dishwasher emptiers get the next month off kitchen duty.
    dishwasher_heroes: int = 2
    # Send the login cookie only over HTTPS. Turn on when the app is behind TLS.
    secure_cookies: bool = False
    # The running release, e.g. "v1.3.0", shown on the admin page. Set by the Docker build
    # from the release tag; empty when run from a checkout.
    app_version: str = ""
    # Off in tests, so no background job touches the test database.
    run_scheduler: bool = True
    # Optional Telegram bot that posts each morning's duties in the floor's group
    # (`services/telegram`). Off unless token and chat are set. The token is the bot's
    # password: whoever has it can post as the bot.
    telegram_bot_token: str = ""
    # The group: its id (-1001234567890), or @name for a public one.
    telegram_chat_id: str = ""
    # A topic of the group to post in (its message_thread_id); without one, the main chat.
    telegram_thread_id: int | None = None
    # The app's address, linked at the end of the bot's posts.
    app_url: str = ""
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

    @field_validator("deadline_hour")
    @classmethod
    def deadline_hour_in_day(cls, value: int) -> int:
        """An hour of the day, 0-23."""
        if not 0 <= value <= 23:
            raise ValueError("DEADLINE_HOUR must be between 0 and 23")
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def bot_token_format(cls, value: str) -> str:
        """Empty (off), or a token like 123456:ABC-def from @BotFather. Spaces and line
        breaks around it (e.g. pasted along) are dropped."""
        value = value.strip()
        if value and not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", value):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN must be a bot token from @BotFather, like 123456:ABC-def"
            )
        return value

    @field_validator("telegram_chat_id")
    @classmethod
    def strip_chat_id(cls, value: str) -> str:
        """The chat id without spaces around it."""
        return value.strip()

    @field_validator("telegram_thread_id", mode="before")
    @classmethod
    def empty_thread_id_is_none(cls, value):
        """`TELEGRAM_THREAD_ID=` (empty) means no topic, not an invalid number."""
        return None if value == "" else value

    @property
    def telegram_enabled(self) -> bool:
        """Whether the Telegram bot is set up."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

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
