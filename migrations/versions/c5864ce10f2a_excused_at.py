"""excused at

Revision ID: c5864ce10f2a
Revises: baebea71425a
Create Date: 2026-11-09 22:02:21.814602

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5864ce10f2a'
down_revision: Union[str, Sequence[str], None] = 'baebea71425a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FKEY = 'potential_drivers_excused_by_id_fkey'


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('potential_drivers', sa.Column('excused_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('potential_drivers', sa.Column('excused_by_id', sa.Integer(), nullable=True))
    op.create_foreign_key(FKEY, 'potential_drivers', 'users', ['excused_by_id'], ['id'], ondelete='SET NULL')
    # Duties excused before: when and by whom is in completed_at / completed_by_id.
    op.execute(
        "UPDATE potential_drivers SET excused_at = completed_at, excused_by_id = completed_by_id "
        "WHERE status = 'excused'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(FKEY, 'potential_drivers', type_='foreignkey')
    op.drop_column('potential_drivers', 'excused_by_id')
    op.drop_column('potential_drivers', 'excused_at')
