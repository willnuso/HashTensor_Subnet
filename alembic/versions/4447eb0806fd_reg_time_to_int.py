"""reg time to int with signature fix

Revision ID: 4447eb0806fd
Revises: e41702caa367
Create Date: 2025-06-17 13:56:02.485972

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import json

# revision identifiers, used by Alembic.
revision: str = '4447eb0806fd'
down_revision: Union[str, None] = 'e41702caa367'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Import verify_signature from your codebase
from src.utils import verify_signature

def upgrade() -> None:
    conn = op.get_bind()
    # 1. Add a new column for the integer registration time (nullable for now)
    op.add_column('hotkey_worker', sa.Column('registration_time_int', sa.BigInteger(), nullable=True))

    # 2. For each row, brute-force the original float value
    rows = list(conn.execute(sa.text("SELECT worker, hotkey, registration_time, signature FROM hotkey_worker")))
    for worker, hotkey, registration_time, signature in rows:
        stored = float(registration_time)
        found = False
        for offset in range(-5, 6):  # Try ±5 microseconds
            candidate_float = stored + offset * 0.000001
            reg_dict = {
                "hotkey": hotkey,
                "worker": worker,
                "registration_time": candidate_float,
            }
            reg_json = json.dumps(reg_dict, sort_keys=True, separators=(",", ":"))
            if verify_signature(hotkey, reg_json, signature):
                reg_time_int = int(candidate_float * 1_000_000)
                conn.execute(
                    sa.text("UPDATE hotkey_worker SET registration_time_int = :reg_time_int WHERE worker = :worker"),
                    {"reg_time_int": reg_time_int, "worker": worker}
                )
                print(f"[OK] Worker: {worker} | Restored: {candidate_float} | Int: {reg_time_int} | Offset: {offset}")
                found = True
                break
        if not found:
            reg_time_int = int(stored * 1_000_000)
            conn.execute(
                sa.text("UPDATE hotkey_worker SET registration_time_int = :reg_time_int WHERE worker = :worker"),
                {"reg_time_int": reg_time_int, "worker": worker}
            )
            print(f"[WARN] Worker: {worker} | Used stored value: {stored} | Int: {reg_time_int} (signature may not verify)")

    # 3. Recreate the table to enforce NOT NULL on registration_time_int (SQLite workaround)
    # Create a new table with the correct schema
    op.create_table(
        "hotkey_worker_new",
        sa.Column("worker", sa.String(), primary_key=True),
        sa.Column("hotkey", sa.String(), nullable=False),
        sa.Column("registration_time", sa.Float(), nullable=False),
        sa.Column("registration_time_int", sa.BigInteger(), nullable=False),
        sa.Column("signature", sa.String(), nullable=False),
    )
    # Copy data
    conn.execute(sa.text(
        "INSERT INTO hotkey_worker_new (worker, hotkey, registration_time, registration_time_int, signature) "
        "SELECT worker, hotkey, registration_time, registration_time_int, signature FROM hotkey_worker"
    ))
    # Drop old table
    op.drop_table("hotkey_worker")
    # Rename new table
    op.rename_table("hotkey_worker_new", "hotkey_worker")

def downgrade() -> None:
    op.drop_column('hotkey_worker', 'registration_time_int')
