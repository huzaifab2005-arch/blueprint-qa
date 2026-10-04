"""chat_messages.count_result for object-count answers

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("count_result", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "count_result")
