"""Adapter: a count question in, the assistant's normal Answer out.

Keeps the router and the stored chat message uniform: a count answer is an Answer
whose `count_result` carries the structured detail (status, per-method numbers,
and the markers the UI draws on the sheet).
"""
import logging
import os

from backend.config import get_settings
from backend.models.assistant import DocumentPage
from backend.models.document import Document
from backend.services.assistant_service import Answer
from backend.services.counting.count_service import (
    NOT_FOUND, CountOutcome, PageMeta, count_objects,
)
from backend.services.counting.objects import ParsedCount
from backend.services.drawing_metadata import page_label
from backend.storage import get_storage

logger = logging.getLogger(__name__)
settings = get_settings()

MAX_MARKERS = 600
DISCLAIMER = (
    "Counts are read from the drawing's own data and are not guaranteed. "
    "Check the marked sheet before relying on a quantity."
)


def to_payload(outcome: CountOutcome) -> dict:
    markers = [m for method in outcome.methods[:1] for m in method.markers]
    truncated = len(markers) > MAX_MARKERS
    return {
        "status": outcome.status,
        "quantity": outcome.quantity,
        "object": outcome.object_label,
        "primary": outcome.primary,
        "methods": [
            {"method": m.method, "quantity": m.quantity, "detail": m.detail,
             "per_page": {str(p): n for p, n in m.per_page.items()}, "breakdown": m.breakdown}
            for m in outcome.methods
        ],
        # Only the headline method's markers: they are the objects the number refers to.
        "markers": [
            {"page_number": m.page_number, "x": m.x, "y": m.y, "w": m.w, "h": m.h, "label": m.label}
            for m in markers[:MAX_MARKERS]
        ],
        "definitions": outcome.definitions,
        "blocking": outcome.blocking,
        "pages_searched": outcome.pages_searched,
        "markers_truncated": truncated,
    }


def _sources(outcome: CountOutcome, metas: dict[int, PageMeta]) -> list[dict]:
    primary = outcome.methods[0] if outcome.methods else None
    if primary is None:
        return []
    sources = []
    for page_number, n in sorted(primary.per_page.items()):
        meta = metas[page_number]
        evidence = [
            {"quote": d["text"][:200], "location": "legend/schedule", "confirmed": True}
            for d in outcome.definitions if d["page_number"] == page_number
        ][:3]
        sources.append({
            "page_number": page_number,
            "sheet_number": meta.sheet_number,
            "sheet_title": meta.sheet_title,
            "label": meta.label,
            "note": f"{n} counted",
            "evidence": evidence,
        })
    return sources


async def answer_count_question(
    document: Document, pages: list[DocumentPage], question: str, parsed: ParsedCount
) -> Answer:
    metas = [PageMeta(p.page_number, p.sheet_number, p.sheet_title, p.text or "", p.image_path) for p in pages]
    by_no = {m.page_number: m for m in metas}

    try:
        pdf_path = await _local_pdf(document)
        outcome = await count_objects(question, parsed, metas, pdf_path)
    except FileNotFoundError:
        return Answer(
            answer="Counting needs the original PDF, which is no longer in storage. Re-upload the drawing to count.",
            verified=False, confidence=None, sources=[], pages_searched=[], warnings=[DISCLAIMER],
        )

    warnings = list(outcome.blocking) + list(outcome.warnings)
    if outcome.status != NOT_FOUND:
        warnings.append(DISCLAIMER)
    answer = Answer(
        answer=outcome.summary,
        verified=outcome.verified,
        confidence=outcome.confidence,
        sources=_sources(outcome, by_no),
        pages_searched=outcome.pages_searched,
        warnings=warnings,
    )
    answer.count_result = to_payload(outcome)
    return answer


async def _local_pdf(document: Document) -> str:
    import asyncio

    path = await asyncio.to_thread(get_storage().get_path, document.file_path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return path
