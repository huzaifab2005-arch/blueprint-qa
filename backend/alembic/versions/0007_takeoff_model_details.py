"""takeoff items: details and model

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("takeoff_items", sa.Column("details", sa.Text(), nullable=False, server_default=""))
    op.add_column("takeoff_items", sa.Column("model", sa.String(300), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("takeoff_items", "model")
    op.drop_column("takeoff_items", "details")
