"""Create the first floor speaker account: uv run python -m scripts.create_admin"""

import asyncio
import getpass
import sys

from app.config import settings
from app.db import SessionLocal
from app.services import duties, users


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
        # Like adding a resident on the admin page: on today's duties if people are missing.
        await duties.fill_current(session, settings.now())
        await session.commit()
        print(f"Floor speaker {user.name} (@{user.username}) created.")


if __name__ == "__main__":
    asyncio.run(main())
