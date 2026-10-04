import uuid
from datetime import datetime
from pydantic import BaseModel, Field


class IndexStatus(BaseModel):
    # not_indexed | indexing | ready | failed
    status: str
    total_pages: int | None = None
    pages_indexed: int = 0
    error: str | None = None


class PageRead(BaseModel):
    page_number: int
    sheet_number: str | None
    sheet_title: str | None
    label: str
    text_source: str
    char_count: int
    width: int | None
    height: int | None


class EvidenceRead(BaseModel):
    quote: str
    location: str = ""
    confirmed: bool = False


class SourceRead(BaseModel):
    page_number: int
    sheet_number: str | None = None
    sheet_title: str | None = None
    label: str
    note: str = ""
    evidence: list[EvidenceRead] = []


class CountMarkerRead(BaseModel):
    """One counted object, as a box in fractions (0..1) of the displayed page."""
    page_number: int
    x: float
    y: float
    w: float
    h: float
    label: str = ""


class CountMethodRead(BaseModel):
    method: str          # tag_instances | symbol | schedule_qty | schedule_rows | vision
    quantity: int
    detail: str
    per_page: dict[str, int] = {}
    breakdown: dict[str, int] = {}


class CountDefinitionRead(BaseModel):
    page_number: int
    label: str
    text: str


class CountResultRead(BaseModel):
    # cross_checked | single_source | needs_verification | not_found
    status: str
    quantity: int | None = None
    object: str
    primary: str | None = None
    methods: list[CountMethodRead] = []
    markers: list[CountMarkerRead] = []
    definitions: list[CountDefinitionRead] = []
    blocking: list[str] = []
    pages_searched: list[int] = []
    markers_truncated: bool = False


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class MessageRead(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    verified: bool | None = None
    confidence: str | None = None
    sources: list[SourceRead] = []
    pages_searched: list[int] = []
    warnings: list[str] = []
    count_result: CountResultRead | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AskResponse(BaseModel):
    question: MessageRead
    answer: MessageRead
