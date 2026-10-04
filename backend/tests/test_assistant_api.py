"""End-to-end: real PDF -> real poppler rendering/text extraction -> HTTP API.

Only two things are faked: the database (SQLite instead of Postgres) and the LLM
(a scripted stand-in that answers from the page text it is shown). That makes it
a test of the pipeline's plumbing and grounding rules, not of model quality.
"""
import io

import pytest

pytest.importorskip("reportlab")
pytest.importorskip("aiosqlite")

import httpx
from reportlab.lib.pagesizes import landscape
from reportlab.pdfgen import canvas
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import get_settings
from backend.database import Base, get_db
from backend.main import app
from backend.services import assistant_service, indexing_service

pytestmark = pytest.mark.asyncio

SHEET = (36 * 72, 24 * 72)


def make_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=SHEET)
    pages = [
        ["COVER SHEET", "DRAWING INDEX", "G0.01"],
        ["LIGHTING SCHEDULE", "TYPE L1  2X4 LED TROFFER  MODEL 24LED-4000  QTY 14 BOH", "E2.01"],
        ["MECHANICAL ROOF PLAN", "RTU-1 TRANE YHC074 7.5 TON", "MAIN SUPPLY DUCT 24x12", "M2.01"],
    ]
    for lines in pages:
        y = SHEET[1] - 100
        for ln in lines[:-1]:
            c.setFont("Helvetica", 28)
            c.drawString(200, y, ln)
            y -= 60
        c.setFont("Helvetica-Bold", 36)
        c.drawString(SHEET[0] - 400, 80, "SHEET NO: " + lines[-1])
        c.showPage()
    c.save()
    return buf.getvalue()


class FakeLLM:
    """Answers from the page text in the prompt; records every call."""
    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    async def __call__(self, model, messages, max_tokens=1200):
        user = messages[-1]["content"]
        text = user if isinstance(user, str) else user[0]["text"]
        has_image = isinstance(user, list) and len(user) > 1
        self.calls.append((text, has_image))
        if "Rewrite the user's latest question" in text:
            return {"question": "what model is RTU-1?"}
        if "Findings from the sheets" in text:
            return {"answerable": True, "answer": "combined", "confidence": "high", "source_pages": [2, 3, 99]}
        if "TRANE YHC074" in text and "RTU-1" in text.split("Question:")[1].split("\n")[0]:
            return {"relevant": True, "answer": "RTU-1 is a TRANE YHC074.", "confidence": "high",
                    "evidence": [{"quote": "RTU-1 TRANE YHC074", "location": "plan"},
                                 {"quote": "INVENTED 9000", "location": "nowhere"}]}
        if "24LED-4000" in text and "light" in text.split("Question:")[1].split("\n")[0].lower():
            return {"relevant": True, "answer": "14 fixtures.", "confidence": "medium",
                    "evidence": [{"quote": "QTY 14", "location": "schedule"}]}
        return {"relevant": False, "answer": "", "evidence": [], "confidence": "low"}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "nvidia_api_key", "nvapi-test")
    monkeypatch.setattr(settings, "assistant_page_image_px", 1200)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(indexing_service, "AsyncSessionLocal", Session)
    llm = FakeLLM()
    monkeypatch.setattr(assistant_service, "_chat_json", llm)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        c.llm = llm
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def upload_and_index(client) -> str:
    r = await client.post("/api/documents/upload", files={"file": ("set.pdf", make_pdf(), "application/pdf")})
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/api/assistant/{doc_id}/index")
    assert r.status_code == 202
    return doc_id


async def test_indexing_extracts_sheets_and_images(client):
    doc_id = await upload_and_index(client)
    st = (await client.get(f"/api/assistant/{doc_id}/index")).json()
    assert st["status"] == "ready" and st["pages_indexed"] == 3, st

    pages = (await client.get(f"/api/assistant/{doc_id}/pages")).json()
    assert [p["sheet_number"] for p in pages] == ["G0.01", "E2.01", "M2.01"]
    assert pages[1]["sheet_title"] == "LIGHTING SCHEDULE"
    assert pages[0]["text_source"] == "text_layer"

    img = await client.get(f"/api/assistant/{doc_id}/pages/2/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert img.content[:2] == b"\xff\xd8"
    assert (await client.get(f"/api/assistant/{doc_id}/pages/9/image")).status_code == 404


async def test_grounded_answer_with_sources(client):
    doc_id = await upload_and_index(client)
    r = await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "What model is RTU-1?"})
    assert r.status_code == 200, r.text
    a = r.json()["answer"]
    assert a["verified"] is True
    assert "TRANE YHC074" in a["content"]
    assert [s["label"] for s in a["sources"]] == ["M2.01"]
    quotes = {e["quote"]: e["confirmed"] for e in a["sources"][0]["evidence"]}
    assert quotes["RTU-1 TRANE YHC074"] is True
    assert quotes["INVENTED 9000"] is False  # fabricated quote is flagged, not trusted
    # Only candidate pages were sent to the model, each with its image, never the whole set.
    page_calls = [c for c in client.llm.calls if "Sheet:" in c[0]]
    assert 0 < len(page_calls) < 3 and all(has_image for _, has_image in page_calls)


async def test_unanswerable_question_is_not_invented(client):
    doc_id = await upload_and_index(client)
    r = await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "What colour is the elephant?"})
    a = r.json()["answer"]
    assert a["verified"] is False and a["sources"] == []
    assert a["content"].startswith("The information could not be verified from the uploaded drawings.")
    assert client.llm.calls == []  # nothing retrieved -> no model call, no chance to hallucinate


async def test_relevant_page_but_model_finds_nothing(client):
    doc_id = await upload_and_index(client)
    # Retrieval will match the mechanical page ("supply"), but the fake model has no answer there.
    r = await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "Who is the supply contractor?"})
    a = r.json()["answer"]
    assert a["verified"] is False and a["pages_searched"]


async def test_multi_page_sources_are_restricted_to_retrieved_pages(client):
    doc_id = await upload_and_index(client)
    r = await client.post(f"/api/assistant/{doc_id}/ask",
                          json={"question": "model of the RTU-1 and the light fixtures 24LED-4000?"})
    a = r.json()["answer"]
    assert a["verified"] is True
    pages = [s["page_number"] for s in a["sources"]]
    assert 99 not in pages and set(pages) <= {2, 3}


async def test_followup_rewrite_history_and_clear(client):
    doc_id = await upload_and_index(client)
    await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "Where is RTU-1 located?"})
    r = await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "what model is it?"})
    assert r.status_code == 200
    assert any("Rewrite" in c[0] for c in client.llm.calls)
    msgs = (await client.get(f"/api/assistant/{doc_id}/messages")).json()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert (await client.delete(f"/api/assistant/{doc_id}/messages")).status_code == 204
    assert (await client.get(f"/api/assistant/{doc_id}/messages")).json() == []


async def test_ask_requires_index_and_valid_document(client):
    r = await client.post("/api/documents/upload", files={"file": ("s.pdf", make_pdf(), "application/pdf")})
    doc_id = r.json()["id"]
    assert (await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "hello?"})).status_code == 409
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.post(f"/api/assistant/{missing}/ask", json={"question": "hello?"})).status_code == 404


async def test_ocr_fallback_for_pages_without_text_layer(client, monkeypatch):
    monkeypatch.setattr(indexing_service, "extract_text_layer", lambda *_: "")
    monkeypatch.setattr(indexing_service, "ocr_page", lambda img: "SHEET NO: E9.99 LIGHTING SCHEDULE " + "x" * 100)
    doc_id = await upload_and_index(client)
    pages = (await client.get(f"/api/assistant/{doc_id}/pages")).json()
    assert {p["text_source"] for p in pages} == {"ocr"}


async def test_delete_document_removes_assistant_data(client, tmp_path):
    doc_id = await upload_and_index(client)
    await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "What model is RTU-1?"})
    assert (await client.delete(f"/api/documents/{doc_id}")).status_code == 204
    assert (await client.get(f"/api/assistant/{doc_id}/pages")).status_code == 404
    leftover = list((tmp_path / "uploads" / "pages").rglob("*.jpg")) if (tmp_path / "uploads" / "pages").exists() else []
    assert leftover == []


async def test_unrelated_question_after_history_is_not_rewritten(client):
    doc_id = await upload_and_index(client)
    await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "What model is RTU-1?"})
    client.llm.calls.clear()
    r = await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "What colour is the elephant?"})
    a = r.json()["answer"]
    assert a["verified"] is False
    assert not any("Rewrite" in c[0] for c in client.llm.calls)
