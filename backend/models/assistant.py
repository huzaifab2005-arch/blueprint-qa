"""Tables for the drawing assistant.

Deliberately separate from `documents`/`issues`: `create_all` only creates
missing tables and never alters existing ones, so adding columns to `documents`
would not reach a database that already has it. New tables are created on
startup (and by migration 0002).
"""
import uuid
from datetime import datetime
from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


class DocumentIndex(Base):
    """Indexing progress for one document (one row per document)."""
    __tablename__ = "document_indexes"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    # indexing | ready | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="indexing")
    total_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DocumentPage(Base):
    """One page of a drawing set: extracted text, sheet metadata, stored image."""
    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number", name="uq_document_page"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sheet_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sheet_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # text_layer | ocr | none
    text_source: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ChatMessage(Base):
    """One turn of the per-document assistant conversation."""
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # assistant only: whether the answer is backed by cited drawing pages
    verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    pages_searched: Mapped[list | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Structured result of an object-count answer (see schemas.assistant.CountResultRead).
    count_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
