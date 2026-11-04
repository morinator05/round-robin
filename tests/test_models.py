"""Model helpers."""

from app.models import User


def test_display_name_uses_first_name_and_room():
    assert User(name="Jean-Luc Picard", room_number="E101").display_name == "Jean-Luc (E101)"
    assert User(name="Worf", room_number="E102").display_name == "Worf (E102)"
