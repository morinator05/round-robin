"""Create the first floor speaker account: uv run python scripts/create_admin.py"""

import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.services import users  # noqa: E402


async def main() -> None:
    name = input("Name: ")
    room = input("Room (e.g. E103): ")
    username = input("Username: ")
    password = getpass.getpass("Password: ")
    async with SessionLocal() as session:
        try:
            user = await users.create(
                session,
                name=name,
                room_number=room,
                username=username,
                password=password,
                is_admin=True,
            )
        except users.UserError as e:
            sys.exit(str(e))
        await session.commit()
        print(f"Floor speaker {user.name} (@{user.username}) created.")


asyncio.run(main())
