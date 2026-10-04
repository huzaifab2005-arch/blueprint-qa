import uuid
import os
from datetime import datetime
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from backend.database import get_db
from backend.models.document import Document, DocumentStatus
from backend.models.issue import Issue
from backend.schemas.document import DocumentRead, DocumentList
from backend.storage import get_storage
from backend.config import get_settings
from backend.services.indexing_service import delete_page_images

router = APIRouter(prefix="/api/documents", tags=["documents"])
settings = get_settings()

MAX_BYTES = settings.max_file_size_mb * 1024 * 1024


@router.post("/upload", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        ext = (file.filename or "").lower().rsplit(".", 1)[-1]
        if ext != "pdf":
            raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    contents = await file.read()
    if len(contents) > MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds maximum allowed size of {settings.max_file_size_mb}MB.",
        )

    # Reset file pointer for storage
    from io import BytesIO
    file.file = BytesIO(contents)

    doc_id = uuid.uuid4()
    safe_filename = f"{doc_id}_{os.path.basename(file.filename or 'upload.pdf')}"
    storage = get_storage()

    # Re-wrap as UploadFile-compatible for storage
    file.file.seek(0)
    file_path = await storage.save(file, safe_filename)

    document = Document(
        id=doc_id,
        filename=file.filename or "upload.pdf",
        file_path=file_path,
        status=DocumentStatus.pending,
        uploaded_at=datetime.utcnow(),
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)

    return DocumentRead(
        id=document.id,
        filename=document.filename,
        file_path=document.file_path,
        page_count=document.page_count,
        status=document.status,
        uploaded_at=document.uploaded_at,
        analyzed_at=document.analyzed_at,
        issue_count=0,
    )


@router.get("", response_model=DocumentList)
async def list_documents(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Document).order_by(Document.uploaded_at.desc()))
    documents = list(result.scalars().all())

    # Get issue counts in bulk
    count_result = await db.execute(
        select(Issue.document_id, func.count(Issue.id).label("cnt"))
        .group_by(Issue.document_id)
    )
    counts = {row.document_id: row.cnt for row in count_result}

    doc_reads = [
        DocumentRead(
            id=d.id,
            filename=d.filename,
            file_path=d.file_path,
            page_count=d.page_count,
            status=d.status,
            uploaded_at=d.uploaded_at,
            analyzed_at=d.analyzed_at,
            issue_count=counts.get(d.id, 0),
        )
        for d in documents
    ]
    return DocumentList(documents=doc_reads, total=len(doc_reads))


@router.get("/{document_id}", response_model=DocumentRead)
async def get_document(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")

    count_result = await db.execute(
        select(func.count(Issue.id)).where(Issue.document_id == document_id)
    )
    issue_count = count_result.scalar_one()

    return DocumentRead(
        id=document.id,
        filename=document.filename,
        file_path=document.file_path,
        page_count=document.page_count,
        status=document.status,
        uploaded_at=document.uploaded_at,
        analyzed_at=document.analyzed_at,
        issue_count=issue_count,
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")

    await delete_page_images(db, document_id)
    storage = get_storage()
    storage.delete(document.file_path)

    await db.delete(document)
    await db.commit()
