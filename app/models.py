"""Database tables (SQLAlchemy). Schema changes need a migration in `migrations/`."""

import enum
import secrets
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class of all tables."""


# Column sizes, checked before saving (`services/users`).
MAX_NAME_LENGTH = 100
MAX_USERNAME_LENGTH = 50


def new_session_token() -> str:
    """A random value for `User.session_token`."""
    return secrets.token_urlsafe(32)


class Duty(str, enum.Enum):
    """The kind of duty; each is a carpool of its own. Kitchen trips are one day, trash
    trips one Monday-Sunday week.
    """

    kitchen = "kitchen"
    trash = "trash"

    @property
    def label(self) -> str:
        """Name shown in the UI."""
        return {"kitchen": "Kitchen duty", "trash": "Trash duty"}[self.value]


class Status(str, enum.Enum):
    """State of one potential driver of a trip."""

    # Nobody has driven the trip yet, the deadline hasn't passed.
    pending = "pending"
    # This person drove (marked it done). Earns them the drive.
    drove = "drove"
    # Deadline passed without anyone driving, or the floor speaker disputed a "drove".
    missed = "missed"
    # The floor speaker let it go: no strike, but no drive either.
    excused = "excused"
    # Someone else drove the trip; this person just rode along.
    rode = "rode"

    @property
    def label(self) -> str:
        """Name shown in the UI."""
        return {
            "pending": "open",
            "drove": "done",
            "missed": "missed",
            "excused": "excused",
            "rode": "not needed",
        }[self.value]


class User(Base):
    """A resident. Deleting a user means they moved out; see `services/users.delete`."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LENGTH))
    room_number: Mapped[str] = mapped_column(String(20))
    username: Mapped[str] = mapped_column(String(MAX_USERNAME_LENGTH), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # Also in the session cookie; a cookie only counts if it matches. A new one (on logout
    # and password changes, `auth.end_sessions`) logs the user out on every device. A
    # database reset gives everyone new ones, so old cookies don't point at new people.
    session_token: Mapped[str] = mapped_column(String(64), default=new_session_token)
    # The floor speaker, who can manage residents, strikes and everybody's duties.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Inactive users are paused: they don't ride, so they are never chosen and owe nothing.
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    away_periods: Mapped[list["AwayPeriod"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    strikes: Mapped[list["Strike"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="Strike.user_id",
    )

    @property
    def first_name(self) -> str:
        """The first word of the name; residents are shown by first name and room."""
        return self.name.split()[0] if self.name.strip() else self.name

    @property
    def display_name(self) -> str:
        """Plain-text form, e.g. "Picard (E101)", for messages and confirm dialogs."""
        return f"{self.first_name} ({self.room_number})"


class AwayPeriod(Base):
    """A time a resident is away (both dates included). They don't ride then."""

    __tablename__ = "away_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)

    user: Mapped[User] = relationship(back_populates="away_periods")


class Trip(Base):
    """One trip of a duty's carpool: a single day for kitchen, a Monday-Sunday week for trash."""

    __tablename__ = "trips"
    __table_args__ = (UniqueConstraint("duty", "start_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    duty: Mapped[Duty] = mapped_column(Enum(Duty, name="duty"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    # Penalty trips are extra work for strikes; they don't count towards fairness.
    is_penalty: Mapped[bool] = mapped_column(Boolean, default=False)

    potential_drivers: Mapped[list["PotentialDriver"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", order_by="PotentialDriver.id"
    )
    riders: Mapped[list["Rider"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan"
    )

    @property
    def driver(self) -> "PotentialDriver | None":
        """Whoever drove the trip (marked it done), if anyone has."""
        return next((pd for pd in self.potential_drivers if pd.status is Status.drove), None)


class Rider(Base):
    """A member who rode on a trip (was available); each is charged an equal fair share."""

    __tablename__ = "riders"
    __table_args__ = (UniqueConstraint("trip_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    # NULL once the user was deleted (moved out). The row stays so the others' shares of
    # past trips don't grow: nobody inherits the work of someone who moved out.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    trip: Mapped[Trip] = relationship(back_populates="riders")


class PotentialDriver(Base):
    """One person chosen as a potential driver of a trip. Kitchen trips have two, trash one."""

    __tablename__ = "potential_drivers"

    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    # NULL once the user was deleted (moved out); the history stays.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[Status] = mapped_column(Enum(Status, name="status"), default=Status.pending)
    # When and by whom it was marked done or excused (the floor speaker can do it for others).
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Set when the floor speaker found it was marked done but not actually done.
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the floor speaker decided a missed duty stays missed (the strike stands); it
    # then no longer needs their attention.
    kept_missed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    trip: Mapped[Trip] = relationship(back_populates="potential_drivers")
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    completed_by: Mapped[User | None] = relationship(foreign_keys=[completed_by_id])


class DishwasherClear(Base):
    """Someone emptied the dishwasher. Each month's top ones become dishwasher heroes
    (`services/dishwasher`)."""

    __tablename__ = "dishwasher_clears"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL once the user was deleted (moved out); the entry no longer counts for anyone.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    cleared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped[User | None] = relationship()


class Strike(Base):
    """A strike against a resident. Enough active ones give a penalty trash week."""

    __tablename__ = "strikes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    # Given by the daily job for a missed duty (not by the floor speaker).
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    # The duty it was given for, if any. Marking that duty done revokes the strike.
    potential_driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("potential_drivers.id", ondelete="SET NULL")
    )
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set when the floor speaker took it back, or the duty was done or excused after all.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the strike was "paid off" by a penalty trash duty (that penalty trip).
    redeemed_by_trip_id: Mapped[int | None] = mapped_column(
        ForeignKey("trips.id", ondelete="SET NULL")
    )

    user: Mapped[User] = relationship(back_populates="strikes", foreign_keys=[user_id])
    issued_by: Mapped[User | None] = relationship(foreign_keys=[issued_by_id])

    @property
    def is_active(self) -> bool:
        """Counts towards the penalty threshold: neither revoked nor paid off."""
        return self.revoked_at is None and self.redeemed_by_trip_id is None
