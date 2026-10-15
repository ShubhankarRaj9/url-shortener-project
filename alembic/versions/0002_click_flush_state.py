"""click flush watermark for exactly-once click counting

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "click_flush_state",
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("last_id", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_click_flush_state")),
    )


def downgrade() -> None:
    op.drop_table("click_flush_state")
