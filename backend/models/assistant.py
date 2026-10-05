"""Tables for the drawing assistant.

Deliberately separate from `documents`/`issues`: `create_all` only creates
missing tables and never alters existing ones, so adding columns to `documents`
would not reach a database that already has it. New tables are created on
startup (and by migration 0002).
"""
import uuid
from datetime import datetime
from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
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


class SheetCalibration(Base):
    """A scale the user set by marking a known dimension on a sheet.

    The most trustworthy scale there is: it comes from a length the user knows. It
    applies near where it was made (a sheet can have several scales), identified by
    the two points that define it.
    """
    __tablename__ = "sheet_calibrations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ratio: Mapped[float] = mapped_column(Float, nullable=False)       # real / paper
    known_length_in: Mapped[float] = mapped_column(Float, nullable=False)
    known_text: Mapped[str] = mapped_column(String(64), nullable=False)   # as the user typed it
    points: Mapped[list] = mapped_column(JSON, nullable=False)           # two [x, y] fractions of the page
    system: Mapped[str] = mapped_column(String(16), nullable=False, default="imperial")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Measurement(Base):
    """A saved measurement. Stores the scale it used and how that scale was checked,
    so a value is never separated from the assumption behind it."""
    __tablename__ = "measurements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)        # length | polyline | area
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    points: Mapped[list] = mapped_column(JSON, nullable=False)           # [[x, y], ...] fractions of the page
    value_in: Mapped[float | None] = mapped_column(Float, nullable=True)       # length / perimeter, inches
    value_sqin: Mapped[float | None] = mapped_column(Float, nullable=True)     # area, square inches
    display: Mapped[str] = mapped_column(String(64), nullable=False)
    display_other: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    uncertainty_in: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    scale_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    scale_text: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    scale_status: Mapped[str] = mapped_column(String(16), nullable=False)       # verified | measured | calibrated | stated | conflict
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TakeoffItem(Base):
    """One line of a quantity takeoff.

    A line records where its quantity came from (a count, saved measurements, or a
    person) and how far to trust it, so a total is never separated from the evidence
    behind it. Status is `verified` only when the source checked itself; a person's
    entry or an edited number is `manual`.
    """
    __tablename__ = "takeoff_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    waste_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)    # count | measurement | manual
    status: Mapped[str] = mapped_column(String(24), nullable=False)         # verified | needs_verification | manual
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # count question / measurement ids
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)      # [{page_number, label, note}]
    basis: Mapped[str] = mapped_column(Text, nullable=False, default="")   # how the number was obtained
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    computed_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)   # before any manual edit
    uncertainty: Mapped[float | None] = mapped_column(Float, nullable=True)         # ± in `unit`, when known
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class CheckDismissal(Base):
    """A set-check finding a reviewer has looked at and set aside.

    Findings are recomputed from the drawings each time, so this stores only the
    finding's stable id; a finding that changes (different sheet, different values)
    gets a new id and shows again."""
    __tablename__ = "check_dismissals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    finding_id: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
