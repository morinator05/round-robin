"""carpool names

Name the tables after the fair carpool algorithm (Fagin & Williams, 1983): slots are
trips, slot_eligible are riders, assignments are potential drivers. The status values
"done" and "covered" become "drove" and "rode". A strike paid off by a penalty week now
points at that penalty trip instead of its assignment.

Revision ID: b7d41c2e9a63
Revises: 9c3e5a7b1d20
Create Date: 2026-11-04 13:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7d41c2e9a63'
down_revision: Union[str, Sequence[str], None] = '9c3e5a7b1d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (old, new) names, applied in this order on upgrade and reversed on downgrade.
TABLES = [
    ("slots", "trips"),
    ("slot_eligible", "riders"),
    ("assignments", "potential_drivers"),
]
# (table after the rename, old column, new column)
COLUMNS = [
    ("riders", "slot_id", "trip_id"),
    ("potential_drivers", "slot_id", "trip_id"),
    ("strikes", "assignment_id", "potential_driver_id"),
]
SEQUENCES = [
    ("slots_id_seq", "trips_id_seq"),
    ("slot_eligible_id_seq", "riders_id_seq"),
    ("assignments_id_seq", "potential_drivers_id_seq"),
]
# (table after the rename, old constraint, new constraint)
CONSTRAINTS = [
    ("trips", "slots_pkey", "trips_pkey"),
    ("trips", "slots_duty_start_date_key", "trips_duty_start_date_key"),
    ("riders", "slot_eligible_pkey", "riders_pkey"),
    ("riders", "slot_eligible_slot_id_fkey", "riders_trip_id_fkey"),
    ("riders", "slot_eligible_user_id_fkey", "riders_user_id_fkey"),
    ("riders", "slot_eligible_slot_id_user_id_key", "riders_trip_id_user_id_key"),
    ("potential_drivers", "assignments_pkey", "potential_drivers_pkey"),
    ("potential_drivers", "assignments_slot_id_fkey", "potential_drivers_trip_id_fkey"),
    ("potential_drivers", "assignments_user_id_fkey", "potential_drivers_user_id_fkey"),
    (
        "potential_drivers",
        "assignments_completed_by_id_fkey",
        "potential_drivers_completed_by_id_fkey",
    ),
    ("strikes", "strikes_assignment_id_fkey", "strikes_potential_driver_id_fkey"),
]
INDEXES = [
    ("ix_slot_eligible_slot_id", "ix_riders_trip_id"),
    ("ix_assignments_slot_id", "ix_potential_drivers_trip_id"),
    ("ix_assignments_user_id", "ix_potential_drivers_user_id"),
]
STATUS_VALUES = [("done", "drove"), ("covered", "rode")]


def upgrade() -> None:
    """Upgrade schema."""
    for old, new in TABLES:
        op.rename_table(old, new)
    for table, old, new in COLUMNS:
        op.alter_column(table, old, new_column_name=new)
    for old, new in SEQUENCES:
        op.execute(f"ALTER SEQUENCE {old} RENAME TO {new}")
    for table, old, new in CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}")
    for old, new in INDEXES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new}")
    for old, new in STATUS_VALUES:
        op.execute(f"ALTER TYPE status RENAME VALUE '{old}' TO '{new}'")

    op.add_column("strikes", sa.Column("redeemed_by_trip_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "strikes_redeemed_by_trip_id_fkey", "strikes", "trips",
        ["redeemed_by_trip_id"], ["id"], ondelete="SET NULL",
    )
    op.execute(
        "UPDATE strikes s SET redeemed_by_trip_id = pd.trip_id FROM potential_drivers pd "
        "WHERE pd.id = s.redeemed_by_assignment_id"
    )
    op.drop_column("strikes", "redeemed_by_assignment_id")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "strikes", sa.Column("redeemed_by_assignment_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "strikes_redeemed_by_assignment_id_fkey", "strikes", "potential_drivers",
        ["redeemed_by_assignment_id"], ["id"], ondelete="SET NULL",
    )
    # A penalty trip has exactly one potential driver.
    op.execute(
        "UPDATE strikes s SET redeemed_by_assignment_id = "
        "(SELECT min(pd.id) FROM potential_drivers pd WHERE pd.trip_id = s.redeemed_by_trip_id) "
        "WHERE s.redeemed_by_trip_id IS NOT NULL"
    )
    op.drop_column("strikes", "redeemed_by_trip_id")

    for old, new in reversed(STATUS_VALUES):
        op.execute(f"ALTER TYPE status RENAME VALUE '{new}' TO '{old}'")
    for old, new in reversed(INDEXES):
        op.execute(f"ALTER INDEX {new} RENAME TO {old}")
    for table, old, new in reversed(CONSTRAINTS):
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old}")
    for old, new in reversed(SEQUENCES):
        op.execute(f"ALTER SEQUENCE {new} RENAME TO {old}")
    for table, old, new in reversed(COLUMNS):
        op.alter_column(table, new, new_column_name=old)
    for old, new in reversed(TABLES):
        op.rename_table(new, old)
