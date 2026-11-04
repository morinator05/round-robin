"""Database tables (SQLAlchemy). Schema changes need a migration in `migrations/`."""

import enum
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


class Duty(str, enum.Enum):
    """The kind of duty; each is a carpool of its own. Kitchen trips are one day, trash
    trips one Monday-Sunday week.
    """

    kitchen = "kitchen"
    trash = "trash"

    @property
    def label(self) -> str:
        """German name shown in the UI."""
        return {"kitchen": "Küchendienst", "trash": "Mülldienst"}[self.value]


class Status(str, enum.Enum):
    """State of one potential driver of a trip."""

    # Nobody has driven the trip yet, the deadline hasn't passed.
    pending = "pending"
    # This person drove (marked it done). Earns them the drive.
    drove = "drove"
    # Deadline passed without anyone driving, or the Flursprecher disputed a "drove".
    missed = "missed"
    # The Flursprecher let it go: no strike, but no drive either.
    excused = "excused"
    # Someone else drove the trip; this person just rode along.
    rode = "rode"

    @property
    def label(self) -> str:
        """German name shown in the UI."""
        return {
            "pending": "offen",
            "drove": "erledigt",
            "missed": "verpasst",
            "excused": "entschuldigt",
            "rode": "nicht nötig",
        }[self.value]


class User(Base):
    """A resident. Deleting a user means they moved out; see `services/users.delete`."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    room_number: Mapped[str] = mapped_column(String(20))
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # The Flursprecher, who can manage residents, strikes and everybody's duties.
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
        """Plain-text form, e.g. "Anna (A401)", for messages and confirm dialogs.

        In page content `person()` in `web.py` is used instead (name and room styled).
        """
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
    # When and by whom it was marked done or excused (the Flursprecher can do it for others).
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Set when the Flursprecher found it was marked done but not actually done.
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    trip: Mapped[Trip] = relationship(back_populates="potential_drivers")
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    completed_by: Mapped[User | None] = relationship(foreign_keys=[completed_by_id])


class Strike(Base):
    """A strike against a resident. Enough active ones give a penalty Mülldienst week."""

    __tablename__ = "strikes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    # Given by the daily job for a missed duty (not by the Flursprecher).
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    # The duty it was given for, if any. Marking that duty done revokes the strike.
    potential_driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("potential_drivers.id", ondelete="SET NULL")
    )
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set when the Flursprecher took it back, or the duty was done or excused after all.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the strike was "paid off" by a penalty Mülldienst (that penalty trip).
    redeemed_by_trip_id: Mapped[int | None] = mapped_column(
        ForeignKey("trips.id", ondelete="SET NULL")
    )

    user: Mapped[User] = relationship(back_populates="strikes", foreign_keys=[user_id])
    potential_driver: Mapped[PotentialDriver | None] = relationship(
        foreign_keys=[potential_driver_id]
    )
    issued_by: Mapped[User | None] = relationship(foreign_keys=[issued_by_id])

    @property
    def is_active(self) -> bool:
        """Counts towards the penalty threshold: neither revoked nor paid off."""
        return self.revoked_at is None and self.redeemed_by_trip_id is None
