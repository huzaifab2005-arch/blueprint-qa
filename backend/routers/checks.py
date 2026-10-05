"""Set checks: cross-sheet findings for a reviewer to look at."""
import asyncio
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.assistant import CheckDismissal, DocumentIndex, DocumentPage
from backend.models.document import Document
from backend.schemas.checks import ChecksRead, DismissRequest, FindingRead
from backend.services.checks.set_checks import PageInfo, run_checks
from backend.services.counting.pdf_geometry import load_geometry
from backend.storage import get_storage

router = APIRouter(prefix="/api/checks", tags=["checks"])

CHECKS_RUN = ["duplicate_sheet", "index_mismatch", "broken_reference", "scale_conflict", "dimension_mismatch"]
DISCLAIMER = ("These are prompts to look, not verdicts. The checks read the drawings' text and linework and can be "
              "wrong; a clean result does not mean the set is correct. Confirm each finding on the sheet.")
MAX_PAGES = 150

# Computing takes a second or two per ten sheets, so results are kept per (pdf, pages).
_CACHE: dict[tuple, tuple] = {}


def _compute(path: str, pages: list[PageInfo]):
    geoms = {}
    skipped = 0
    for p in pages[:MAX_PAGES]:
        try:
            geoms[p.page_number] = load_geometry(path, p.page_number)
        except Exception:
            skipped += 1
    skipped += max(0, len(pages) - MAX_PAGES)
    return run_checks(pages, geoms), len(geoms), skipped


async def _doc(db: AsyncSession, document_id: uuid.UUID) -> Document:
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return doc


@router.get("/{document_id}", response_model=ChecksRead)
async def get_checks(document_id: uuid.UUID, include_dismissed: bool = False, db: AsyncSession = Depends(get_db)):
    doc = await _doc(db, document_id)
    index = await db.get(DocumentIndex, document_id)
    if index is None or index.status != "ready":
        raise HTTPException(status_code=409, detail="This document is not indexed yet. Index it first.")
    rows = (await db.execute(
        select(DocumentPage).where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_number)
    )).scalars().all()
    pages = [PageInfo(r.page_number, r.sheet_number, r.sheet_title, r.text or "") for r in rows]
    try:
        path = await asyncio.to_thread(get_storage().get_path, doc.file_path)
        key = (path, os.path.getmtime(path), tuple((p.page_number, p.sheet_number) for p in pages))
    except (FileNotFoundError, OSError):
        raise HTTPException(status_code=409, detail="The original PDF is no longer in storage, so the set cannot be checked.")
    if key not in _CACHE:
        if len(_CACHE) > 16:
            _CACHE.clear()
        _CACHE[key] = await asyncio.to_thread(_compute, path, pages)
    findings, checked, skipped = _CACHE[key]

    dismissed = set((await db.execute(
        select(CheckDismissal.finding_id).where(CheckDismissal.document_id == document_id)
    )).scalars().all())
    out = []
    for f in findings:
        is_d = f.id in dismissed
        if is_d and not include_dismissed:
            continue
        box = dict(zip("xywh", map(float, f.box))) if f.box else None
        out.append(FindingRead(id=f.id, check=f.check, severity=f.severity, confidence=f.confidence,
                               page_number=f.page_number, label=f.label, message=f.message, evidence=f.evidence,
                               box=box, dismissed=is_d))
    open_n = sum(1 for f in out if not f.dismissed)
    summary = (f"{open_n} thing{'s' if open_n != 1 else ''} to look at across {checked} sheet{'s' if checked != 1 else ''}."
               if open_n else f"Nothing flagged across {checked} sheets. That is not a guarantee the set is correct.")
    return ChecksRead(findings=out, pages_checked=checked, pages_skipped=skipped, checks_run=CHECKS_RUN,
                      summary=summary, disclaimer=DISCLAIMER)


@router.post("/{document_id}/dismissals", status_code=status.HTTP_201_CREATED)
async def dismiss(document_id: uuid.UUID, body: DismissRequest, db: AsyncSession = Depends(get_db)):
    await _doc(db, document_id)
    exists = (await db.execute(select(CheckDismissal).where(
        CheckDismissal.document_id == document_id, CheckDismissal.finding_id == body.finding_id))).scalar_one_or_none()
    if exists is None:
        db.add(CheckDismissal(document_id=document_id, finding_id=body.finding_id))
        await db.commit()
    return {"finding_id": body.finding_id}


@router.delete("/{document_id}/dismissals", status_code=status.HTTP_204_NO_CONTENT)
async def restore(document_id: uuid.UUID, finding_id: str, db: AsyncSession = Depends(get_db)):
    await _doc(db, document_id)
    await db.execute(delete(CheckDismissal).where(
        CheckDismissal.document_id == document_id, CheckDismissal.finding_id == finding_id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
