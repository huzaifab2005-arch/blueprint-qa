"""Turns an uploaded PDF into searchable, viewable pages.

Per page: render an image, extract text (the PDF's embedded text layer when it
has one, OCR otherwise), detect the sheet number and title, store the image and
write a `document_pages` row. Progress is committed per page so the UI can show
it while a large set is still processing.

Pages are rendered one at a time: a 36x24in sheet is tens of megapixels, so
holding a whole set in memory (as the QA pipeline does for 10 pages) would not
scale to a 100-sheet set.
"""
import asyncio
import io
import logging
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from backend.config import get_settings
from backend.database import AsyncSessionLocal
from backend.models.assistant import DocumentIndex, DocumentPage
from backend.models.document import Document
from backend.services.drawing_metadata import detect_sheet_metadata
from backend.storage import get_storage

logger = logging.getLogger(__name__)
settings = get_settings()

# An "indexing" row older than this is assumed to belong to a dead process
# (indexing runs in-process, so a restart abandons it) and may be restarted.
STALE_AFTER = timedelta(minutes=30)


@dataclass
class ProcessedPage:
    text: str
    text_source: str  # text_layer | ocr | none
    image_bytes: bytes
    width: int
    height: int
    # Reading-order text, where title blocks come out label-anchored (see
    # drawing_metadata.detect_title_block). Empty for OCR'd pages.
    raw_text: str = ""


def page_image_key(document_id: uuid.UUID, page_number: int) -> str:
    return f"pages/{document_id}/{page_number}.jpg"


# Thumbnails are cached in storage at a few fixed widths, so they can be cleaned up.
THUMB_WIDTHS = (240, 360, 480, 640)


def snap_thumb_width(w: int) -> int:
    return min(THUMB_WIDTHS, key=lambda t: abs(t - w))


def thumbnail_key(document_id: uuid.UUID, page_number: int, width: int) -> str:
    return f"pages/{document_id}/thumb_{page_number}_{width}.jpg"


def _delete_thumbnails(storage, document_id: uuid.UUID, page_number: int) -> None:
    for w in THUMB_WIDTHS:
        try:
            storage.delete(storage.locate(thumbnail_key(document_id, page_number, w)))
        except Exception:
            pass


def count_pdf_pages(pdf_path: str) -> int:
    from pdf2image import pdfinfo_from_path
    return int(pdfinfo_from_path(pdf_path)["Pages"])


def extract_text_layer(pdf_path: str, page_number: int, mode: str = "-layout") -> str:
    """Embedded text for one page via poppler's pdftotext (empty if none/failed).

    mode "-layout" keeps tables aligned (best for reading); "-raw" gives content
    order, which keeps a title block's label and value adjacent."""
    try:
        out = subprocess.run(
            ["pdftotext", "-f", str(page_number), "-l", str(page_number), mode, pdf_path, "-"],
            capture_output=True, timeout=60, check=True,
        )
        return out.stdout.decode("utf-8", errors="replace").strip()
    except Exception as exc:
        logger.warning("pdftotext failed for page %s: %s", page_number, exc)
        return ""


def ocr_page(image) -> str:
    """OCR tuned for drawings: sparse-text mode finds scattered labels and tags
    that the default page-segmentation mode skips as 'not a paragraph'."""
    try:
        import pytesseract
        return pytesseract.image_to_string(image, config="--psm 11", timeout=180).strip()
    except Exception as exc:
        logger.warning("OCR failed: %s", exc)
        return ""


def process_page(pdf_path: str, page_number: int) -> ProcessedPage:
    """Blocking: render, extract text, encode. Run in a worker thread."""
    from pdf2image import convert_from_path

    images = convert_from_path(
        pdf_path,
        first_page=page_number,
        last_page=page_number,
        size=settings.assistant_page_image_px,  # int = longest side in px
    )
    if not images:
        raise RuntimeError(f"Could not render page {page_number}")
    image = images[0].convert("RGB")

    text = extract_text_layer(pdf_path, page_number)
    source = "text_layer"
    if len(text) < settings.assistant_text_layer_min_chars:
        ocr_text = ocr_page(image)
        if len(ocr_text) > len(text):
            text, source = ocr_text, "ocr"
        elif not text:
            source = "none"

    raw_text = extract_text_layer(pdf_path, page_number, "-raw") if source == "text_layer" else ""

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return ProcessedPage(text, source, buf.getvalue(), image.width, image.height, raw_text)


async def begin_indexing(db, document_id: uuid.UUID) -> tuple[DocumentIndex, bool]:
    """Create/reset the index row. Returns (row, should_start).

    should_start is False when a live indexing run already exists.
    """
    row = (await db.execute(
        select(DocumentIndex).where(DocumentIndex.document_id == document_id)
    )).scalar_one_or_none()

    if row and row.status == "indexing" and row.started_at > datetime.utcnow() - STALE_AFTER:
        return row, False

    if row is None:
        row = DocumentIndex(document_id=document_id)
        db.add(row)
    row.status = "indexing"
    row.total_pages = None
    row.pages_indexed = 0
    row.error = None
    row.started_at = datetime.utcnow()
    row.completed_at = None
    await db.commit()
    return row, True


async def _set_failed(document_id: uuid.UUID, message: str) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(DocumentIndex, document_id)
        if row:
            row.status = "failed"
            row.error = message[:1000]
            row.completed_at = datetime.utcnow()
            await db.commit()


async def run_indexing(document_id: uuid.UUID) -> None:
    """Background task entry point. Owns its own DB session."""
    storage = get_storage()
    try:
        async with AsyncSessionLocal() as db:
            document = await db.get(Document, document_id)
            if document is None:
                return
            pdf_path = storage.get_path(document.file_path)

            # Drop any previous index (re-index), including stored images.
            old = (await db.execute(
                select(DocumentPage.page_number, DocumentPage.image_path).where(DocumentPage.document_id == document_id)
            )).all()
            for old_no, path in old:
                _delete_thumbnails(storage, document_id, old_no)
                if path:
                    try:
                        storage.delete(path)
                    except Exception:
                        pass
            await db.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))
            await db.commit()

            total = await asyncio.to_thread(count_pdf_pages, pdf_path)
            limited = min(total, settings.assistant_max_index_pages)
            row = await db.get(DocumentIndex, document_id)
            row.total_pages = limited
            if total > limited:
                row.error = (
                    f"Only the first {limited} of {total} pages were indexed "
                    f"(ASSISTANT_MAX_INDEX_PAGES)."
                )
            await db.commit()

            for page_number in range(1, limited + 1):
                processed = await asyncio.to_thread(process_page, pdf_path, page_number)
                image_path = await asyncio.to_thread(
                    storage.save_bytes, processed.image_bytes, page_image_key(document_id, page_number)
                )
                sheet, title = detect_sheet_metadata(processed.raw_text, processed.text)
                db.add(DocumentPage(
                    document_id=document_id,
                    page_number=page_number,
                    sheet_number=sheet,
                    sheet_title=title,
                    text_source=processed.text_source,
                    text=processed.text,
                    image_path=image_path,
                    width=processed.width,
                    height=processed.height,
                ))
                row.pages_indexed = page_number
                await db.commit()
                logger.info(
                    "Indexed %s page %s/%s (%s, %d chars, sheet=%s)",
                    document_id, page_number, limited, processed.text_source,
                    len(processed.text), sheet,
                )

            row.status = "ready"
            row.completed_at = datetime.utcnow()
            await db.commit()
    except Exception as exc:
        logger.exception("Indexing failed for %s", document_id)
        await _set_failed(document_id, f"{type(exc).__name__}: {exc}")


async def delete_page_images(db, document_id: uuid.UUID) -> None:
    """Best-effort removal of stored page images (DB rows cascade on their own)."""
    storage = get_storage()
    rows = (await db.execute(
        select(DocumentPage.page_number, DocumentPage.image_path).where(DocumentPage.document_id == document_id)
    )).all()
    for page_no, path in rows:
        _delete_thumbnails(storage, document_id, page_no)
        if path:
            try:
                storage.delete(path)
            except Exception as exc:
                logger.warning("Could not delete page image %s: %s", path, exc)
