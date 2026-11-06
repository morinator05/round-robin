"""session tokens

Each user gets a random session token, which the session cookie must carry. A new token
logs the user out on every device. Existing users get one here, so everybody logs in once
after this migration (old cookies have no token).

Revision ID: c5e1a9d47b20
Revises: b7d41c2e9a63
Create Date: 2026-11-06 13:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c5e1a9d47b20'
down_revision: Union[str, Sequence[str], None] = 'b7d41c2e9a63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("session_token", sa.String(length=64), nullable=True))
    # Random per row; the app makes new ones with `secrets.token_urlsafe`.
    op.execute("UPDATE users SET session_token = md5(random()::text || id::text)")
    op.alter_column("users", "session_token", nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "session_token")
