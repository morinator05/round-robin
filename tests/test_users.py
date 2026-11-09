"""Residents: names, rooms, passwords, absences and the first floor speaker."""

from datetime import timedelta

import pytest

from app.config import settings
from app.models import User
from app.services import users
from app.services.users import UserError
from tests.helpers import MONDAY, at, make_users


def test_resident_fields():
    assert User(name="Jean-Luc Picard", room_number="E101").display_name == "Jean-Luc (E101)"
    assert users.check_room(" e103 ") == "E103"
    for valid in ["E101", "E199", "B110"]:
        assert users.check_room(valid) == valid
    for invalid in ["E10", "E100", "E.121", "E1.1", "E1100", "1103", "EE103", ""]:
        with pytest.raises(UserError):
            users.check_room(invalid)


async def test_account_limits(session, monkeypatch):
    with pytest.raises(UserError):
        await users.create(session, name="A", room_number="E101", username="a", password="short")

    # The first floor speaker comes from the settings, only into an empty database.
    monkeypatch.setattr(settings, "admin_username", "")  # whatever the local .env says
    assert await users.create_initial_admin(session) is None
    for key, value in dict(username="Chef", password="secret123", name="Chef", room="E150").items():
        monkeypatch.setattr(settings, f"admin_{key}", value)
    admin = await users.create_initial_admin(session)
    assert admin.is_admin and admin.username == "chef"
    await session.commit()
    # Not again once there are users, e.g. after the admin was deleted and others were added.
    assert await users.create_initial_admin(session) is None

    # Away periods end at most a year ahead.
    [resident] = await make_users(session, n=1, admin=False)
    far = MONDAY + timedelta(days=users.MAX_AWAY_DAYS_AHEAD + 1)
    with pytest.raises(UserError):
        await users.add_away(session, resident, MONDAY, far, at(MONDAY))
    await users.add_away(session, resident, MONDAY, far - timedelta(days=1), at(MONDAY))
