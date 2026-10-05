from pydantic import BaseModel, Field


class FindingRead(BaseModel):
    id: str
    check: str
    severity: str            # high | medium | low (how much it matters if real)
    confidence: str          # high | medium | low (how sure the check is)
    page_number: int
    label: str
    message: str
    evidence: list[str] = []
    box: dict[str, float] | None = None
    dismissed: bool = False


class ChecksRead(BaseModel):
    findings: list[FindingRead]
    pages_checked: int
    pages_skipped: int = 0
    checks_run: list[str]
    summary: str
    disclaimer: str


class DismissRequest(BaseModel):
    finding_id: str = Field(min_length=3, max_length=200)
