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
    created_at: datetime

    model_config = {"from_attributes": True}


class AskResponse(BaseModel):
    question: MessageRead
    answer: MessageRead
