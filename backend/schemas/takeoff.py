import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SourceRead(BaseModel):
    page_number: int
    label: str = ""
    note: str = ""


class TakeoffItemRead(BaseModel):
    id: uuid.UUID
    category: str
    description: str
    quantity: float
    unit: str
    waste_pct: float
    order_quantity: float
    source_kind: str            # count | measurement | manual
    status: str                 # verified | needs_verification | manual
    confidence: str | None = None
    basis: str = ""
    sources: list[SourceRead] = []
    warnings: list[str] = []
    computed_quantity: float | None = None
    uncertainty: float | None = None
    notes: str = ""
    created_at: datetime
    updated_at: datetime


class UnitTotal(BaseModel):
    lines: int
    verified_lines: int
    needs_verification: int
    manual: int
    quantity: float
    with_waste: float
    verified_quantity: float


class CategoryUnit(BaseModel):
    quantity: float
    with_waste: float
    lines: int


class TakeoffRead(BaseModel):
    items: list[TakeoffItemRead]
    lines: int
    verified_lines: int
    needs_verification_lines: int
    manual_lines: int
    by_unit: dict[str, UnitTotal]
    by_category: dict[str, dict[str, CategoryUnit]]
    disclaimer: str


class FromCountRequest(BaseModel):
    # Either re-run a count question, or reuse a count the assistant already answered.
    question: str | None = Field(None, min_length=2, max_length=2000)
    message_id: uuid.UUID | None = None
    category: str = Field("", max_length=80)
    description: str | None = Field(None, max_length=200)


class FromMeasurementsRequest(BaseModel):
    measurement_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=200)
    category: str = Field("", max_length=80)


class ManualItemCreate(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    quantity: float = Field(ge=0, le=1e9)
    unit: str = Field(min_length=1, max_length=16)
    category: str = Field("", max_length=80)
    waste_pct: float = Field(0, ge=0, le=100)
    notes: str = Field("", max_length=2000)


class TakeoffItemUpdate(BaseModel):
    description: str | None = Field(None, min_length=1, max_length=200)
    category: str | None = Field(None, max_length=80)
    quantity: float | None = Field(None, ge=0, le=1e9)
    unit: str | None = Field(None, min_length=1, max_length=16)
    waste_pct: float | None = Field(None, ge=0, le=100)
    notes: str | None = Field(None, max_length=2000)
