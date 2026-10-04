"""Drawing assistant API: indexing, page access and conversational Q&A."""
import asyncio
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import get_db
from backend.models.assistant import ChatMessage, DocumentIndex, DocumentPage
from backend.models.document import Document
from backend.schemas.assistant import (
    AskRequest, AskResponse, IndexStatus, MessageRead, PageRead,
)
from backend.services.assistant_service import (
    AssistantError, NotIndexed, answer_question,
)
from backend.services.counting.count_answer import answer_count_question
from backend.services.counting.objects import is_count_question, parse_count_question
from backend.services.drawing_metadata import page_label
from backend.services.indexing_service import begin_indexing, run_indexing
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

    parsed = parse_count_question(question) if is_count_question(question) else None
    # Counting reads the PDF directly and works without a key (its optional vision
    # estimate checks for one itself); only the language-model path requires it.
    if parsed is None and not settings.nvidia_api_key:
        raise HTTPException(status_code=503, detail="NVIDIA_API_KEY is not configured on the server.")
    try:
        if parsed is not None:
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
