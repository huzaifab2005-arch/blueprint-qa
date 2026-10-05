"""Drawing assistant API: indexing, page access and conversational Q&A."""
import asyncio
import uuid

import io

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import get_db
from backend.models.assistant import ChatMessage, DocumentIndex, DocumentPage
from backend.models.document import Document
from backend.schemas.assistant import (
    AskRequest, AskResponse, BoxRead, HighlightsRead, IndexStatus, MessageRead, PageRead,
    ReferenceRead, SearchResponseRead, SearchResultRead, SnippetRead,
)
from backend.services.assistant_service import (
    Answer, AssistantError, NotIndexed, answer_question,
)
from backend.services.counting.count_answer import answer_count_question
from backend.services.counting.count_service import PageMeta
from backend.services.takeoff.answer import MENU, build_answer
from backend.services.takeoff.disciplines import is_takeoff_request, parse_takeoff_request
from backend.services.counting.objects import is_count_question, parse_count_question
from backend.services.counting.pdf_geometry import PageGeometry, load_geometry
from backend.services.drawing_metadata import normalise_sheet_key, page_label
from backend.services.grounding import alnum
from backend.services.navigation_service import find_highlights, find_references
from backend.services.search_service import PageRecord, parse_query, search_pages
from backend.services.indexing_service import begin_indexing, run_indexing, snap_thumb_width, thumbnail_key
from backend.storage import get_storage

router = APIRouter(prefix="/api/assistant", tags=["assistant"])
settings = get_settings()


async def _require_document(db: AsyncSession, document_id: uuid.UUID) -> Document:
    document = await db.get(Document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")
    return document


def _status(row: DocumentIndex | None) -> IndexStatus:
    if row is None:
        return IndexStatus(status="not_indexed")
    return IndexStatus(
        status=row.status,
        total_pages=row.total_pages,
        pages_indexed=row.pages_indexed,
        error=row.error,
    )


@router.post("/{document_id}/index", response_model=IndexStatus, status_code=status.HTTP_202_ACCEPTED)
async def start_indexing(
    document_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Start (or restart) indexing. Idempotent while a run is in progress."""
    await _require_document(db, document_id)
    row, should_start = await begin_indexing(db, document_id)
    if should_start:
        background_tasks.add_task(run_indexing, document_id)
    return _status(row)


@router.get("/{document_id}/index", response_model=IndexStatus)
async def get_index_status(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _require_document(db, document_id)
    return _status(await db.get(DocumentIndex, document_id))


@router.get("/{document_id}/pages", response_model=list[PageRead])
async def list_pages(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _require_document(db, document_id)
    pages = (await db.execute(
        select(DocumentPage)
        .where(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number)
    )).scalars().all()
    return [
        PageRead(
            page_number=p.page_number,
            sheet_number=p.sheet_number,
            sheet_title=p.sheet_title,
            label=page_label(p.page_number, p.sheet_number),
            text_source=p.text_source,
            char_count=len(p.text or ""),
            width=p.width,
            height=p.height,
        )
        for p in pages
    ]


@router.get("/{document_id}/pages/{page_number}/image")
async def get_page_image(
    document_id: uuid.UUID, page_number: int, db: AsyncSession = Depends(get_db)
):
    page = (await db.execute(
        select(DocumentPage).where(
            DocumentPage.document_id == document_id,
            DocumentPage.page_number == page_number,
        )
    )).scalar_one_or_none()
    if not page or not page.image_path:
        raise HTTPException(status_code=404, detail="Page image not found.")
    try:
        data = await asyncio.to_thread(get_storage().read_bytes, page.image_path)
    except Exception:
        # Typically ephemeral storage that was wiped by a restart/redeploy.
        raise HTTPException(
            status_code=404,
            detail="Page image is no longer in storage. Re-index the document to regenerate it.",
        )
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/{document_id}/messages", response_model=list[MessageRead])
async def get_messages(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _require_document(db, document_id)
    rows = (await db.execute(
        select(ChatMessage)
        .where(ChatMessage.document_id == document_id)
        .order_by(ChatMessage.created_at, ChatMessage.role.desc())
    )).scalars().all()
    return [_message_read(m) for m in rows]


@router.delete("/{document_id}/messages", status_code=status.HTTP_204_NO_CONTENT)
async def clear_messages(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _require_document(db, document_id)
    await db.execute(delete(ChatMessage).where(ChatMessage.document_id == document_id))
    await db.commit()


@router.post("/{document_id}/ask", response_model=AskResponse)
async def ask(document_id: uuid.UUID, body: AskRequest, db: AsyncSession = Depends(get_db)):
    document = await _require_document(db, document_id)

    index = await db.get(DocumentIndex, document_id)
    if index is None or index.status != "ready":
        raise HTTPException(
            status_code=409,
            detail="This document is not indexed yet. Start indexing and wait for it to finish.",
        )
    question = body.question.strip()
    history = list(reversed((await db.execute(
        select(ChatMessage)
        .where(ChatMessage.document_id == document_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.role.asc())
        .limit(settings.assistant_history_messages)
    )).scalars().all()))

    takeoff = is_takeoff_request(question)
    parsed = parse_count_question(question) if (is_count_question(question) and not takeoff) else None
    # Counting and takeoffs read the PDF directly and work without a key (counting's optional
    # vision estimate checks for one itself); only the language-model path requires it.
    if parsed is None and not takeoff and not settings.nvidia_api_key:
        raise HTTPException(status_code=503, detail="NVIDIA_API_KEY is not configured on the server.")
    try:
        if takeoff:
            from backend.routers.takeoff import generate_discipline
            disc = parse_takeoff_request(question)
            if disc is None:
                result = Answer(answer=MENU, verified=False, confidence=None, sources=[], pages_searched=[], warnings=[])
            else:
                gen = await generate_discipline(db, document, disc)
                rows = (await db.execute(
                    select(DocumentPage).where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_number)
                )).scalars().all()
                result = build_answer(gen, {r.page_number: PageMeta(r.page_number, r.sheet_number, r.sheet_title, "") for r in rows})
        elif parsed is not None:
            pages = list((await db.execute(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_number)
            )).scalars().all())
            if not pages:
                raise NotIndexed("This document has not been indexed yet.")
            result = await answer_count_question(document, pages, question, parsed)
        else:
            result = await answer_question(db, document_id, question, history)
    except NotIndexed as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except AssistantError as exc:
        raise HTTPException(status_code=502, detail=f"AI service error: {exc}")

    # Persist only once the turn succeeded, so a failed call leaves no orphan question.
    user_msg = ChatMessage(document_id=document_id, role="user", content=question)
    db.add(user_msg)
    await db.flush()
    assistant_msg = ChatMessage(
        document_id=document_id,
        role="assistant",
        content=result.answer,
        verified=result.verified,
        confidence=result.confidence,
        sources=result.sources,
        pages_searched=result.pages_searched,
        warnings=result.warnings,
        count_result=result.count_result,
    )
    db.add(assistant_msg)
    await db.commit()
    await db.refresh(user_msg)
    await db.refresh(assistant_msg)
    return AskResponse(question=_message_read(user_msg), answer=_message_read(assistant_msg))


def _message_read(m: ChatMessage) -> MessageRead:
    return MessageRead(
        id=m.id,
        role=m.role,
        content=m.content,
        verified=m.verified,
        confidence=m.confidence,
        sources=m.sources or [],
        pages_searched=m.pages_searched or [],
        warnings=m.warnings or [],
        count_result=m.count_result,
        created_at=m.created_at,
    )


# ── Search and visual navigation ────────────────────────────────────────────

@router.get("/{document_id}/search", response_model=SearchResponseRead)
async def search_document(
    document_id: uuid.UUID,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(40, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Literal search across every indexed sheet (see services/search_service.py)."""
    await _require_document(db, document_id)
    pages = (await db.execute(
        select(DocumentPage).where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_number)
    )).scalars().all()
    resp = search_pages(
        [PageRecord(p.page_number, p.text or "", p.sheet_number, p.sheet_title) for p in pages], q, limit)
    return SearchResponseRead(
        query=resp.query, mode=resp.mode, suggestion=resp.suggestion, total_pages=resp.total_pages,
        results=[
            SearchResultRead(
                page_number=r.page_number, label=r.label, sheet_number=r.sheet_number, sheet_title=r.sheet_title,
                score=round(r.score, 3), match_count=r.match_count, kind=r.kind, title_match=r.title_match,
                snippets=[SnippetRead(text=sn.text, spans=[list(sp) for sp in sn.spans]) for sn in r.snippets],
            )
            for r in resp.results
        ],
    )


async def _page_geometry(db: AsyncSession, document_id: uuid.UUID, page_number: int) -> tuple[DocumentPage, PageGeometry]:
    document = await _require_document(db, document_id)
    page = (await db.execute(
        select(DocumentPage).where(
            DocumentPage.document_id == document_id, DocumentPage.page_number == page_number)
    )).scalar_one_or_none()
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found.")
    try:
        path = await asyncio.to_thread(get_storage().get_path, document.file_path)
        geom = await asyncio.to_thread(load_geometry, path, page_number)
    except (FileNotFoundError, OSError):
        raise HTTPException(
            status_code=404,
            detail="The original PDF is no longer in storage, so positions on the sheet are unavailable.")
    return page, geom


@router.get("/{document_id}/pages/{page_number}/highlights", response_model=HighlightsRead)
async def page_highlights(
    document_id: uuid.UUID,
    page_number: int,
    q: str = Query("", max_length=200),
    phrase: list[str] = Query(default=[], max_length=10),
    db: AsyncSession = Depends(get_db),
):
    """Where a search query (and/or exact phrases, e.g. a cited quote) appears on one sheet."""
    _page, geom = await _page_geometry(db, document_id, page_number)
    pq = parse_query(q) if q.strip() else parse_query("")
    phrases = list(pq.phrases) + [alnum(p) for p in phrase if alnum(p)]
    boxes = find_highlights(geom, pq.terms, phrases, patterns=pq.patterns)
    return HighlightsRead(page_number=page_number, boxes=[BoxRead(**b.__dict__) for b in boxes])


@router.get("/{document_id}/pages/{page_number}/references", response_model=list[ReferenceRead])
async def page_references(document_id: uuid.UUID, page_number: int, db: AsyncSession = Depends(get_db)):
    """References from this sheet to other sheets in the set, with where they sit on the page."""
    page, geom = await _page_geometry(db, document_id, page_number)
    all_pages = (await db.execute(
        select(DocumentPage).where(DocumentPage.document_id == document_id)
    )).scalars().all()
    sheets = {
        normalise_sheet_key(p.sheet_number): (p.page_number, page_label(p.page_number, p.sheet_number), p.sheet_title)
        for p in all_pages if p.sheet_number
    }
    own = normalise_sheet_key(page.sheet_number) if page.sheet_number else None
    refs = find_references(geom, sheets, own)
    return [
        ReferenceRead(target_page=r.target_page, target_label=r.target_label, target_title=r.target_title,
                      text=r.text, box=BoxRead(**r.box.__dict__))
        for r in refs
    ]


@router.get("/{document_id}/pages/{page_number}/thumbnail")
async def page_thumbnail(
    document_id: uuid.UUID,
    page_number: int,
    w: int = Query(360, ge=120, le=800, description="snapped to 240, 360, 480 or 640"),
    db: AsyncSession = Depends(get_db),
):
    """A small JPEG of a sheet for the sheet grid; made on first request, then cached."""
    await _require_document(db, document_id)
    page = (await db.execute(
        select(DocumentPage).where(
            DocumentPage.document_id == document_id, DocumentPage.page_number == page_number)
    )).scalar_one_or_none()
    if not page or not page.image_path:
        raise HTTPException(status_code=404, detail="Page image not found.")
    storage = get_storage()
    w = snap_thumb_width(w)
    key = thumbnail_key(document_id, page_number, w)
    headers = {"Cache-Control": "private, max-age=86400"}
    try:
        return Response(await asyncio.to_thread(storage.read_bytes, storage.locate(key)), media_type="image/jpeg", headers=headers)
    except Exception:
        pass  # not cached yet

    def make() -> bytes:
        from PIL import Image
        img = Image.open(io.BytesIO(storage.read_bytes(page.image_path))).convert("RGB")
        img.thumbnail((w, w * 2))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=78, optimize=True)
        return buf.getvalue()

    try:
        data = await asyncio.to_thread(make)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail="Page image is no longer in storage. Re-index the document to regenerate it.")
    try:
        await asyncio.to_thread(storage.save_bytes, data, key)
    except Exception:
        pass  # caching is an optimisation
    return Response(data, media_type="image/jpeg", headers=headers)
