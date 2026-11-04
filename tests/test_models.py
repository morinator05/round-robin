"""Model helpers."""

from app.models import User


def test_display_name_uses_first_name_and_room():
    assert User(name="Ben Müller", room_number="A401").display_name == "Ben (A401)"
    assert User(name="Ben", room_number="A402").display_name == "Ben (A402)"
