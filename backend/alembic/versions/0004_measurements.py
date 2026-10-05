"""measurements and sheet calibrations

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sheet_calibrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("ratio", sa.Float(), nullable=False),
        sa.Column("known_length_in", sa.Float(), nullable=False),
        sa.Column("known_text", sa.String(64), nullable=False),
        sa.Column("points", sa.JSON(), nullable=False),
        sa.Column("system", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sheet_calibrations_document_id", "sheet_calibrations", ["document_id"])
    op.create_table(
        "measurements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("points", sa.JSON(), nullable=False),
        sa.Column("value_in", sa.Float(), nullable=True),
        sa.Column("value_sqin", sa.Float(), nullable=True),
        sa.Column("display", sa.String(64), nullable=False),
        sa.Column("display_other", sa.String(64), nullable=False),
        sa.Column("uncertainty_in", sa.Float(), nullable=False),
        sa.Column("scale_ratio", sa.Float(), nullable=False),
        sa.Column("scale_text", sa.String(160), nullable=False),
        sa.Column("scale_status", sa.String(16), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_measurements_document_id", "measurements", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_measurements_document_id", table_name="measurements")
    op.drop_table("measurements")
    op.drop_index("ix_sheet_calibrations_document_id", table_name="sheet_calibrations")
    op.drop_table("sheet_calibrations")
