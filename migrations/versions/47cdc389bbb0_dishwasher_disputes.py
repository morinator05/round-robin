"""dishwasher disputes

Revision ID: 47cdc389bbb0
Revises: c5864ce10f2a
Create Date: 2026-11-19 08:46:23.500422

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '47cdc389bbb0'
down_revision: Union[str, Sequence[str], None] = 'c5864ce10f2a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DISPUTED_FKEY = 'dishwasher_clears_disputed_by_id_fkey'
CONFIRMED_FKEY = 'dishwasher_clears_confirmed_by_id_fkey'


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('dishwasher_clears', sa.Column('disputed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('dishwasher_clears', sa.Column('disputed_by_id', sa.Integer(), nullable=True))
    op.add_column('dishwasher_clears', sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('dishwasher_clears', sa.Column('confirmed_by_id', sa.Integer(), nullable=True))
    op.create_foreign_key(DISPUTED_FKEY, 'dishwasher_clears', 'users', ['disputed_by_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(CONFIRMED_FKEY, 'dishwasher_clears', 'users', ['confirmed_by_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(CONFIRMED_FKEY, 'dishwasher_clears', type_='foreignkey')
    op.drop_constraint(DISPUTED_FKEY, 'dishwasher_clears', type_='foreignkey')
    op.drop_column('dishwasher_clears', 'confirmed_by_id')
    op.drop_column('dishwasher_clears', 'confirmed_at')
    op.drop_column('dishwasher_clears', 'disputed_by_id')
    op.drop_column('dishwasher_clears', 'disputed_at')
