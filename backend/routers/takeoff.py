"""Quantity takeoff: lines built from counts, measurements or manual entries."""
import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.assistant import ChatMessage, DocumentIndex, DocumentPage, Measurement, TakeoffItem
from backend.models.document import Document
from backend.schemas.takeoff import (
    FromCountRequest, FromMeasurementsRequest, GenerateRequest, GenerateResult, ManualItemCreate, TakeoffItemRead,
    TakeoffItemUpdate, TakeoffRead,
)
from backend.services.counting.count_answer import _local_pdf, to_payload
from backend.services.counting.count_service import PageMeta, count_objects
from backend.services.counting.objects import is_count_question, parse_count_question
from backend.services.drawing_metadata import page_label
from backend.services.takeoff import lines
from backend.services.takeoff.disciplines import DISCIPLINES, Discipline
from backend.services.takeoff.discipline_takeoff import build_discipline_takeoff
from backend.services.takeoff.lines import LineDraft, TakeoffError

router = APIRouter(prefix="/api/takeoff", tags=["takeoff"])


async def _document(db: AsyncSession, document_id: uuid.UUID) -> Document:
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return doc


async def _item(db: AsyncSession, document_id: uuid.UUID, item_id: uuid.UUID) -> TakeoffItem:
    it = await db.get(TakeoffItem, item_id)
    if not it or it.document_id != document_id:
        raise HTTPException(status_code=404, detail="Takeoff line not found.")
    return it


def _read(it: TakeoffItem) -> TakeoffItemRead:
    return TakeoffItemRead(
        id=it.id, category=it.category, description=it.description, details=it.details or "", model=it.model or "",
        source=lines.source_text(it.sources or []), quantity=it.quantity, unit=it.unit, waste_pct=it.waste_pct, order_quantity=lines.quantity_with_waste(it.quantity, it.waste_pct, it.unit),
        source_kind=it.source_kind, status=it.status, confidence=it.confidence, basis=it.basis,
        sources=it.sources or [], warnings=it.warnings or [], computed_quantity=it.computed_quantity,
        uncertainty=it.uncertainty, notes=it.notes, created_at=it.created_at, updated_at=it.updated_at,
    )


async def _all(db: AsyncSession, document_id: uuid.UUID) -> list[TakeoffItem]:
    return list((await db.execute(
        select(TakeoffItem).where(TakeoffItem.document_id == document_id).order_by(TakeoffItem.created_at)
    )).scalars().all())


def _apply(it: TakeoffItem, d: LineDraft) -> None:
    it.quantity = d.quantity
    it.computed_quantity = d.quantity
    it.unit = d.unit
    it.status = d.status
    it.confidence = d.confidence
    it.basis = d.basis
    it.source_ref = d.source_ref
    it.sources = d.sources
    it.warnings = d.warnings
    it.uncertainty = d.uncertainty


async def _run_count(db: AsyncSession, document: Document, question: str) -> dict:
    """Count payload for a question, exactly as the assistant would answer it."""
    if not is_count_question(question):
        question = f"how many {question}"
    parsed = parse_count_question(question)
    if parsed is None:
        raise HTTPException(status_code=422, detail="Could not tell what to count from that. Name the object, e.g. 'recessed downlights'.")
    index = await db.get(DocumentIndex, document.id)
    if index is None or index.status != "ready":
        raise HTTPException(status_code=409, detail="This document is not indexed yet. Index it first.")
    pages = (await db.execute(
        select(DocumentPage).where(DocumentPage.document_id == document.id).order_by(DocumentPage.page_number)
    )).scalars().all()
    metas = [PageMeta(p.page_number, p.sheet_number, p.sheet_title, p.text or "", p.image_path) for p in pages]
    try:
        pdf = await _local_pdf(document)
    except FileNotFoundError:
        raise HTTPException(status_code=409, detail="Counting needs the original PDF, which is no longer in storage.")
    outcome = await count_objects(question, parsed, metas, pdf)
    payload = to_payload(outcome)
    payload["_labels"] = {m.page_number: m.label for m in metas}
    return payload


def _label_sources(d: LineDraft, labels: dict[int, str]) -> None:
    for s in d.sources:
        s["label"] = labels.get(s["page_number"], s["label"])


async def _labels(db: AsyncSession, document_id: uuid.UUID) -> dict[int, str]:
    rows = (await db.execute(
        select(DocumentPage.page_number, DocumentPage.sheet_number).where(DocumentPage.document_id == document_id)
    )).all()
    return {n: page_label(n, sheet) for n, sheet in rows}


@router.get("/{document_id}", response_model=TakeoffRead)
async def get_takeoff(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _document(db, document_id)
    items = await _all(db, document_id)
    return TakeoffRead(items=[_read(i) for i in items], **lines.summarise(items))


@router.post("/{document_id}/items/from-count", response_model=TakeoffItemRead, status_code=status.HTTP_201_CREATED)
async def add_from_count(document_id: uuid.UUID, body: FromCountRequest, db: AsyncSession = Depends(get_db)):
    document = await _document(db, document_id)
    if bool(body.question) == bool(body.message_id):
        raise HTTPException(status_code=422, detail="Give either a question to count or the id of a count answer.")
    if body.message_id:
        msg = await db.get(ChatMessage, body.message_id)
        if not msg or msg.document_id != document_id or not msg.count_result:
            raise HTTPException(status_code=404, detail="That message is not a count answer for this document.")
        payload = msg.count_result
        prior = (await db.execute(
            select(ChatMessage).where(ChatMessage.document_id == document_id, ChatMessage.role == "user",
                                      ChatMessage.created_at <= msg.created_at)
            .order_by(ChatMessage.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        question = prior.content if prior else f"how many {payload.get('object', '')}"
        labels = await _labels(db, document_id)
    else:
        question = body.question.strip()
        payload = await _run_count(db, document, question)
        labels = payload.pop("_labels")
    try:
        d = lines.line_from_count(payload, question)
    except TakeoffError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _label_sources(d, labels)
    it = TakeoffItem(document_id=document_id, category=body.category.strip(),
                     description=(body.description or d.description).strip(), waste_pct=0.0, notes="",
                     source_kind=d.source_kind, quantity=d.quantity, unit=d.unit, status=d.status, basis="")
    _apply(it, d)
    db.add(it)
    await db.commit()
    await db.refresh(it)
    return _read(it)


@router.post("/{document_id}/items/from-measurements", response_model=TakeoffItemRead, status_code=status.HTTP_201_CREATED)
async def add_from_measurements(document_id: uuid.UUID, body: FromMeasurementsRequest, db: AsyncSession = Depends(get_db)):
    await _document(db, document_id)
    ms = list((await db.execute(
        select(Measurement).where(Measurement.document_id == document_id, Measurement.id.in_(body.measurement_ids))
    )).scalars().all())
    if len(ms) != len(set(body.measurement_ids)):
        raise HTTPException(status_code=404, detail="One or more measurements were not found on this document.")
    try:
        d = lines.line_from_measurements(ms, body.description.strip(), await _labels(db, document_id))
    except TakeoffError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    it = TakeoffItem(document_id=document_id, category=body.category.strip(), description=d.description,
                     waste_pct=0.0, notes="", source_kind=d.source_kind, quantity=d.quantity, unit=d.unit,
                     status=d.status, basis="")
    _apply(it, d)
    db.add(it)
    await db.commit()
    await db.refresh(it)
    return _read(it)


@router.post("/{document_id}/items", response_model=TakeoffItemRead, status_code=status.HTTP_201_CREATED)
async def add_manual(document_id: uuid.UUID, body: ManualItemCreate, db: AsyncSession = Depends(get_db)):
    await _document(db, document_id)
    it = TakeoffItem(
        document_id=document_id, category=body.category.strip(), description=body.description.strip(),
        quantity=body.quantity, unit=lines.normalise_unit(body.unit), waste_pct=body.waste_pct,
        source_kind="manual", status=lines.MANUAL, confidence=None, basis="Entered by a person; not read from the drawing.",
        sources=[], warnings=[], notes=body.notes,
    )
    db.add(it)
    await db.commit()
    await db.refresh(it)
    return _read(it)


@router.patch("/{document_id}/items/{item_id}", response_model=TakeoffItemRead)
async def update_item(document_id: uuid.UUID, item_id: uuid.UUID, body: TakeoffItemUpdate, db: AsyncSession = Depends(get_db)):
    it = await _item(db, document_id, item_id)
    if body.description is not None:
        it.description = body.description.strip()
    if body.category is not None:
        it.category = body.category.strip()
    if body.waste_pct is not None:
        it.waste_pct = body.waste_pct
    if body.notes is not None:
        it.notes = body.notes
    if body.model is not None:
        it.model = body.model.strip()
    if body.details is not None:
        it.details = body.details.strip()
    if body.unit is not None:
        unit = lines.normalise_unit(body.unit)
        if it.source_kind != "manual" and unit != it.unit:
            raise HTTPException(status_code=422, detail="The unit of a line taken from the drawing cannot be changed; "
                                                        "add a manual line instead.")
        it.unit = unit
    if body.quantity is not None and body.quantity != it.quantity:
        it.quantity = body.quantity
        if it.source_kind != "manual":
            ref = dict(it.source_ref or {})
            if it.status != lines.MANUAL:
                ref["original"] = {"status": it.status, "confidence": it.confidence}
            if it.computed_quantity is not None and abs(body.quantity - it.computed_quantity) < 1e-9:
                orig = ref.pop("original", None) or {}
                it.status, it.confidence = orig.get("status", it.status), orig.get("confidence", it.confidence)
            else:
                it.status, it.confidence = lines.MANUAL, None
            it.source_ref = ref
    await db.commit()
    await db.refresh(it)
    return _read(it)


@router.delete("/{document_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(document_id: uuid.UUID, item_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    it = await _item(db, document_id, item_id)
    await db.delete(it)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{document_id}/items/{item_id}/refresh", response_model=TakeoffItemRead)
async def refresh_item(document_id: uuid.UUID, item_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Recompute a line from its source (the drawing may have been re-uploaded, or
    measurements changed). A quantity a person edited is kept; the new computed
    value is shown beside it."""
    document = await _document(db, document_id)
    it = await _item(db, document_id, item_id)
    ref = it.source_ref or {}
    overridden = it.status == lines.MANUAL and it.source_kind != "manual"
    old_computed = it.computed_quantity
    if it.source_kind == "manual":
        raise HTTPException(status_code=422, detail="A manual line has no source to refresh from.")
    if it.source_kind == "discipline":
        raise HTTPException(status_code=422, detail="This line came from a discipline takeoff. Generate that takeoff "
                                                    "again to refresh it.")
    if it.source_kind == "count":
        payload = await _run_count(db, document, ref.get("question") or f"how many {it.description}")
        labels = payload.pop("_labels")
        try:
            d = lines.line_from_count(payload, ref.get("question", ""))
        except TakeoffError as exc:
            raise HTTPException(status_code=422, detail=f"The count can no longer be reproduced: {exc}")
        _label_sources(d, labels)
    else:
        ids = [uuid.UUID(x) for x in ref.get("measurement_ids", [])]
        ms = list((await db.execute(
            select(Measurement).where(Measurement.document_id == document_id, Measurement.id.in_(ids))
        )).scalars().all())
        if not ms:
            raise HTTPException(status_code=409, detail="The measurements behind this line have been deleted.")
        d = lines.line_from_measurements(ms, it.description, await _labels(db, document_id))
        if len(ms) < len(ids):
            d.warnings.append(f"{len(ids) - len(ms)} measurement(s) behind this line were deleted; the total covers the rest.")
    kept_qty, kept_status, kept_conf = it.quantity, it.status, it.confidence
    _apply(it, d)
    if overridden:
        it.quantity, it.status, it.confidence = kept_qty, kept_status, kept_conf
        it.source_ref = {**d.source_ref, "original": {"status": d.status, "confidence": d.confidence}}
        it.warnings = d.warnings + [f"Quantity was edited by a person; the drawing now gives {d.quantity:g} {d.unit}."]
    elif old_computed is not None and abs(old_computed - d.quantity) > 1e-9:
        it.warnings = d.warnings + [f"Changed from {old_computed:g} to {d.quantity:g} on refresh."]
    await db.commit()
    await db.refresh(it)
    return _read(it)


@router.get("/{document_id}/export.csv", response_class=PlainTextResponse)
async def export_csv(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    document = await _document(db, document_id)
    items = await _all(db, document_id)
    stem = (document.filename or "takeoff").rsplit(".", 1)[0].replace('"', "")
    return PlainTextResponse(
        "﻿" + lines.to_csv(items), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{stem}-takeoff.csv"'},
    )


async def generate_discipline(db: AsyncSession, document: Document, disc: Discipline, category: str | None = None) -> GenerateResult:
    """Build (or rebuild) one discipline's lines from the drawings.

    Lines are matched to the previous run by item key. A line a person edited keeps their
    quantity (the drawing's value is shown beside it); untouched lines are replaced, and
    untouched lines the drawings no longer produce are removed."""
    index = await db.get(DocumentIndex, document.id)
    if index is None or index.status != "ready":
        raise HTTPException(status_code=409, detail="This document is not indexed yet. Index it first.")
    pages = (await db.execute(
        select(DocumentPage).where(DocumentPage.document_id == document.id).order_by(DocumentPage.page_number)
    )).scalars().all()
    metas = [PageMeta(p.page_number, p.sheet_number, p.sheet_title, p.text or "", p.image_path) for p in pages]
    try:
        pdf = await _local_pdf(document)
    except FileNotFoundError:
        raise HTTPException(status_code=409, detail="A takeoff needs the original PDF, which is no longer in storage.")
    result = await asyncio.to_thread(build_discipline_takeoff, pdf, metas, disc)

    existing = {
        (it.source_ref or {}).get("key"): it for it in await _all(db, document.id)
        if it.source_kind == "discipline" and (it.source_ref or {}).get("discipline") == disc.key
    }
    cat = (category if category is not None else disc.label).strip()
    added = updated = kept = 0
    fresh: set[str] = set()
    out: list[TakeoffItem] = []
    for item in result.items:
        fresh.add(item.key)
        ref = {"discipline": disc.key, "key": item.key, "entry": item.entry}
        prev = existing.get(item.key)
        if prev is None:
            it = TakeoffItem(document_id=document.id, category=cat, description=item.item[:200], quantity=float(item.quantity),
                             unit=item.unit, waste_pct=0.0, source_kind="discipline", status=item.status, notes="")
            db.add(it)
            added += 1
        else:
            it = prev
            updated += 1
        edited = prev is not None and prev.status == lines.MANUAL
        keep_qty, keep_status, keep_conf = it.quantity, it.status, it.confidence
        it.computed_quantity = float(item.quantity)
        it.details, it.model, it.basis = item.details[:2000], item.model[:300], item.basis
        it.sources, it.source_ref, it.unit = item.sources, ref, item.unit
        it.warnings = list(item.warnings)
        if edited:
            it.quantity, it.status, it.confidence = keep_qty, keep_status, keep_conf
            it.source_ref = {**ref, "original": {"status": item.status, "confidence": item.confidence}}
            it.warnings.append(f"Quantity was edited by a person; the drawing now gives {item.quantity:g} {item.unit}.")
            kept += 1
        else:
            it.quantity, it.status, it.confidence = float(item.quantity), item.status, item.confidence
        out.append(it)
    removed = 0
    for k, it in existing.items():
        if k not in fresh:
            if it.status == lines.MANUAL:
                it.warnings = list(it.warnings or []) + ["The drawings no longer produce this item; kept because you edited it."]
                kept += 1
            else:
                await db.delete(it)
                removed += 1
    await db.commit()
    for it in out:
        await db.refresh(it)
    return GenerateResult(discipline=disc.key, label=disc.label, added=added, updated=updated, removed=removed,
                          kept_edited=kept, notes=result.notes, items=[_read(i) for i in out])


@router.post("/{document_id}/generate", response_model=GenerateResult)
async def generate(document_id: uuid.UUID, body: GenerateRequest, db: AsyncSession = Depends(get_db)):
    document = await _document(db, document_id)
    return await generate_discipline(db, document, DISCIPLINES[body.discipline], body.category)
