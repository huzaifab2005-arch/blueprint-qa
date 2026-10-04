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


DEFAULT_PAGES = [
    ["COVER SHEET", "DRAWING INDEX", "G0.01"],
    ["LIGHTING SCHEDULE", "TYPE L1  2X4 LED TROFFER  MODEL 24LED-4000  QTY 14 BOH", "E2.01"],
    ["MECHANICAL ROOF PLAN", "RTU-1 TRANE YHC074 7.5 TON", "MAIN SUPPLY DUCT 24x12", "M2.01"],
]


def make_pdf(pages=None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=SHEET)
    pages = pages or DEFAULT_PAGES
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


async def upload_and_index(client, pages=None) -> str:
    r = await client.post("/api/documents/upload", files={"file": ("set.pdf", make_pdf(pages), "application/pdf")})
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


# ── Phase 1 reliability ─────────────────────────────────────────────────────

async def test_hallucinated_model_number_is_rejected(client, monkeypatch):
    async def liar(model, messages, max_tokens=1200):
        text = messages[-1]["content"][0]["text"] if isinstance(messages[-1]["content"], list) else ""
        if "Sheet: M2.01" in text:
            return {"relevant": True, "answer": "RTU-1 is a CARRIER 50XC060.", "confidence": "high",
                    "evidence": [{"quote": "CARRIER 50XC060", "location": "plan"}]}
        return {"relevant": False, "answer": "", "evidence": [], "confidence": "low"}
    monkeypatch.setattr(assistant_service, "_chat_json", liar)
    doc_id = await upload_and_index(client)
    a = (await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "What model is RTU-1?"})).json()["answer"]
    assert a["verified"] is False and a["sources"] == []
    assert a["content"].startswith("The information could not be verified")
    assert any("discarded" in w and "50XC060" in w for w in a["warnings"])


async def test_unconfirmed_evidence_caps_confidence(client, monkeypatch):
    async def sloppy(model, messages, max_tokens=1200):
        text = messages[-1]["content"][0]["text"] if isinstance(messages[-1]["content"], list) else ""
        if "Sheet: M2.01" in text:  # answer is on the sheet, but the quote is paraphrased
            return {"relevant": True, "answer": "RTU-1 is a TRANE YHC074.", "confidence": "high",
                    "evidence": [{"quote": "unit one is trane", "location": "plan"}]}
        return {"relevant": False, "answer": "", "evidence": [], "confidence": "low"}
    monkeypatch.setattr(assistant_service, "_chat_json", sloppy)
    doc_id = await upload_and_index(client)
    a = (await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "What model is RTU-1?"})).json()["answer"]
    assert a["verified"] is True and a["confidence"] == "low"
    assert any("quotes were not found" in w for w in a["warnings"])


async def test_synthesis_cannot_add_unsupported_details(client, monkeypatch):
    llm = client.llm

    async def inventive(model, messages, max_tokens=1200):
        text = messages[-1]["content"] if isinstance(messages[-1]["content"], str) else messages[-1]["content"][0]["text"]
        if "Findings from the sheets" in text:
            return {"answerable": True, "answer": "Lights are 24LED-4000 and RTU-1 is a LENNOX LGH120H4.",
                    "confidence": "high", "source_pages": [2, 3]}
        return await llm(model, messages, max_tokens)
    monkeypatch.setattr(assistant_service, "_chat_json", inventive)
    doc_id = await upload_and_index(client)
    a = (await client.post(f"/api/assistant/{doc_id}/ask",
                           json={"question": "model of the RTU-1 and the light fixtures 24LED-4000?"})).json()["answer"]
    assert "LENNOX" not in a["content"]
    assert "TRANE YHC074" in a["content"]
    assert any("not found in the sheets" in w for w in a["warnings"])


async def test_overflow_and_unreadable_pages_are_disclosed(client, monkeypatch):
    pages = [[f"MECHANICAL PLAN {i}", "RTU-1 TRANE YHC074", f"M2.0{i}"] for i in range(1, 7)]
    pages.append(["SCANNED SHEET", "", "A1.01"])
    real = indexing_service.extract_text_layer
    monkeypatch.setattr(indexing_service, "extract_text_layer", lambda path, n, mode="-layout": "" if n == 7 else real(path, n, mode))
    monkeypatch.setattr(indexing_service, "ocr_page", lambda img: "")
    doc_id = await upload_and_index(client, pages)
    settings = get_settings()
    a = (await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "What model is RTU-1?"})).json()["answer"]
    assert len(a["pages_searched"]) == settings.assistant_top_k
    assert len(pages) > settings.assistant_top_k  # the premise: more sheets matched than are read
    assert any("also matched but were not read" in w for w in a["warnings"])
    assert any("no readable text" in w for w in a["warnings"])


async def test_transient_llm_errors_are_retried(monkeypatch):
    import openai
    calls = {"n": 0}
    err = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))

    async def flaky(model, messages, max_tokens):
        calls["n"] += 1
        if calls["n"] == 1:
            raise err
        if calls["n"] == 2:
            return None  # unparseable
        return {"ok": True}

    async def no_sleep(_):
        return None
    monkeypatch.setattr(assistant_service, "_call_json", flaky)
    monkeypatch.setattr(assistant_service, "_sleep", no_sleep)
    assert await assistant_service._chat_json("m", []) == {"ok": True}
    assert calls["n"] == 3

    async def always_down(model, messages, max_tokens):
        raise err
    monkeypatch.setattr(assistant_service, "_call_json", always_down)
    with pytest.raises(openai.APIConnectionError):
        await assistant_service._chat_json("m", [])


async def test_eval_harness_full_mode_against_inprocess_api(client, monkeypatch, tmp_path):
    """The harness itself: upload, index, ask, score, clean up, with scripted answers."""
    from backend.evals import run_eval
    from backend.evals.sample_set import build_sample_pdf

    async def scripted(model, messages, max_tokens=1200):
        last = messages[-1]["content"]
        text = last if isinstance(last, str) else last[0]["text"]
        if "Sheet: M6.01" in text and "Question: What model is RTU-1?" in text:
            return {"relevant": True, "answer": "RTU-1 is a TRANE YHC074.", "confidence": "high",
                    "evidence": [{"quote": "RTU-1  TRANE         YHC074", "location": "RTU schedule"}]}
        if "Sheet: M6.01" in text and "Question: What model is the chiller?" in text:
            # A model that invents an answer from an unrelated schedule: grounding must stop it.
            return {"relevant": True, "answer": "The chiller is a TRANE CVHF2000.", "confidence": "high",
                    "evidence": [{"quote": "TRANE CVHF2000", "location": "schedule"}]}
        return {"relevant": False, "answer": "", "evidence": [], "confidence": "low"}
    monkeypatch.setattr(assistant_service, "_chat_json", scripted)

    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(build_sample_pdf())
    cases = [
        {"id": "pos", "question": "What model is RTU-1?", "expected_sheets": ["M6.01"], "must_include": ["YHC074"]},
        {"id": "neg", "question": "What model is the chiller?", "expect_unverified": True},
    ]
    results = await run_eval.run_full(str(pdf), cases, "http://t", transport=httpx.ASGITransport(app=app))
    assert [r.ok for r in results] == [True, True], [r.detail for r in results]
    s = run_eval.summarise(results)
    assert s["false_answer_rate"] == 0 and s["answer_accuracy"] == 1.0


# ── Phase 3: object counting through the API ────────────────────────────────

async def _upload_count_set(client, **kwargs):
    from backend.evals.count_sample import build_count_pdf

    r = await client.post("/api/documents/upload",
                          files={"file": ("count.pdf", build_count_pdf(**kwargs), "application/pdf")})
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    await client.post(f"/api/assistant/{doc_id}/index")
    assert (await client.get(f"/api/assistant/{doc_id}/index")).json()["status"] == "ready"
    return doc_id


async def test_count_answer_is_cross_checked_with_markers_sources_and_persistence(client):
    doc_id = await _upload_count_set(client)
    r = await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "How many 2x4 lights?"})
    assert r.status_code == 200, r.text
    a = r.json()["answer"]
    cr = a["count_result"]
    assert cr["status"] == "cross_checked" and cr["quantity"] == 8 and cr["primary"] == "tag_instances"
    assert {m["method"] for m in cr["methods"]} == {"tag_instances", "symbol", "schedule_qty"}
    assert len(cr["markers"]) == 8 and all(0 <= m["x"] <= 1 and 0 <= m["y"] <= 1 for m in cr["markers"])
    assert a["verified"] is True and a["confidence"] == "high"
    assert [s["label"] for s in a["sources"]] == ["E2.01"] and a["sources"][0]["note"] == "8 counted"
    assert a["content"].startswith("8 ") and "not guaranteed" not in a["content"]
    quotes = [e["quote"] for e in a["sources"][0]["evidence"]]
    assert quotes and not any(q.startswith("Legend:") for q in quotes)         # locatable on the sheet
    assert any("not guaranteed" in w for w in a["warnings"])          # the standing disclaimer
    assert not client.llm.calls                                       # no model call was needed

    saved = [m for m in (await client.get(f"/api/assistant/{doc_id}/messages")).json() if m["role"] == "assistant"]
    assert saved[0]["count_result"]["quantity"] == 8                  # survives a reload


async def test_count_with_an_area_filter_is_not_called_verified(client):
    doc_id = await _upload_count_set(client)
    a = (await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "How many 2x4 lights in BOH?"})).json()["answer"]
    assert a["count_result"]["status"] == "needs_verification" and a["verified"] is False
    assert any("BOH" in b for b in a["count_result"]["blocking"])
    assert "needs verification" in a["content"]


async def test_count_not_found_and_disagreement(client):
    doc_id = await _upload_count_set(client)
    a = (await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "How many diffusers?"})).json()["answer"]
    assert a["count_result"]["status"] == "not_found" and a["count_result"]["quantity"] is None and a["verified"] is False

    bad = await _upload_count_set(client, schedule_qty_l1=9)
    a = (await client.post(f"/api/assistant/{bad}/ask", json={"question": "How many 2x4 lights?"})).json()["answer"]
    assert a["count_result"]["status"] == "needs_verification" and a["verified"] is False
    assert any("disagree" in w for w in a["warnings"])


async def test_non_count_questions_still_use_document_qa(client):
    doc_id = await _upload_count_set(client)
    a = (await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "Which page contains the lighting schedule?"})).json()["answer"]
    assert a["count_result"] is None


async def test_count_without_the_pdf_explains_instead_of_failing(client, tmp_path):
    import shutil
    doc_id = await _upload_count_set(client)
    shutil.rmtree(tmp_path / "uploads", ignore_errors=True)            # ephemeral storage wiped
    r = await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "How many 2x4 lights?"})
    assert r.status_code == 200
    a = r.json()["answer"]
    assert a["verified"] is False and "original PDF" in a["content"]


async def test_eval_scores_counts_and_flags_false_verified():
    from backend.evals.run_eval import score_answer, summarise
    case = {"id": "c", "question": "q", "expected_count": 8, "expected_status": ["cross_checked"]}
    good = {"count_result": {"quantity": 8, "status": "cross_checked"}}
    wrong_but_confident = {"count_result": {"quantity": 9, "status": "single_source"}}
    wrong_and_flagged = {"count_result": {"quantity": 9, "status": "needs_verification"}}
    assert score_answer(case, good).ok
    r = score_answer(case, wrong_but_confident)
    assert not r.ok and r.checks["false_verified"] and "FALSE VERIFIED" in r.detail
    assert not score_answer(case, wrong_and_flagged).checks["false_verified"]   # wrong, but honestly flagged
    s = summarise([score_answer(case, good), r])
    assert s["false_verified_rate"] == 0.5 and s["count_accuracy"] == 0.5


async def test_existing_chat_table_gets_the_new_column():
    """create_all never alters an existing table: a database created before this feature
    must be upgraded in place, not fail on its first insert."""
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from backend.database import _ensure_columns

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, content TEXT)"))
        await conn.run_sync(_ensure_columns)
        cols = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("chat_messages")})
        await conn.run_sync(_ensure_columns)                        # idempotent
    assert "count_result" in cols
    await engine.dispose()


# ── Real-endpoint robustness: large inline images ───────────────────────────

def test_fit_b64_squeezes_a_noisy_image_under_the_limit():
    import base64, io, os
    from PIL import Image
    img = Image.frombytes("RGB", (1600, 1035), os.urandom(1600 * 1035 * 3))   # worst case for JPEG
    big = assistant_service._fit_b64(img, 10_000_000)
    small = assistant_service._fit_b64(img, 170_000)
    assert len(small) < len(big) and len(small) <= 170_000
    assert Image.open(io.BytesIO(base64.b64decode(small))).size[0] >= 64        # still a decodable JPEG


async def test_vision_json_retries_smaller_when_the_endpoint_rejects_the_image(client, monkeypatch, tmp_path):
    import io
    import httpx
    import openai
    from PIL import Image

    path = tmp_path / "uploads" / "p.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2000, 1300), "white").save(path, format="JPEG")
    sizes = []

    async def fake(model, messages, max_tokens=1200):
        url = messages[-1]["content"][1]["image_url"]["url"]
        sizes.append(len(url))
        if len(sizes) == 1:
            req = httpx.Request("POST", "http://x")
            raise openai.BadRequestError("image too large", response=httpx.Response(400, request=req), body=None)
        return {"ok": True}
    monkeypatch.setattr(assistant_service, "_chat_json", fake)
    monkeypatch.setattr(assistant_service.settings, "assistant_model_image_px", 1600)

    assert await assistant_service.vision_json("q", str(path)) == {"ok": True}
    assert len(sizes) == 2 and sizes[1] <= assistant_service.RETRY_IMAGE_B64_BYTES + 100

    # A non-size error (auth) is not retried with a smaller image: it is raised.
    async def unauthorized(model, messages, max_tokens=1200):
        raise openai.AuthenticationError("bad key", response=httpx.Response(401, request=httpx.Request("POST", "http://x")), body=None)
    monkeypatch.setattr(assistant_service, "_chat_json", unauthorized)
    with pytest.raises(openai.AuthenticationError):
        await assistant_service.vision_json("q", str(path))


async def test_counting_works_without_an_api_key_but_language_answers_do_not(client, monkeypatch):
    doc_id = await _upload_count_set(client)
    monkeypatch.setattr(get_settings(), "nvidia_api_key", "")
    ok = await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "How many 2x4 lights?"})
    assert ok.status_code == 200 and ok.json()["answer"]["count_result"]["quantity"] == 8
    qa = await client.post(f"/api/assistant/{doc_id}/ask", json={"question": "Which page has the lighting schedule?"})
    assert qa.status_code == 503 and "NVIDIA_API_KEY" in qa.json()["detail"]


# ── Phase 2: search and visual navigation through the API ───────────────────

async def _upload_numeric_set(client):
    from backend.evals.sample_set import build_numeric_titleblock_pdf

    r = await client.post("/api/documents/upload",
                          files={"file": ("n.pdf", build_numeric_titleblock_pdf(), "application/pdf")})
    doc_id = r.json()["id"]
    await client.post(f"/api/assistant/{doc_id}/index")
    assert (await client.get(f"/api/assistant/{doc_id}/index")).json()["status"] == "ready"
    return doc_id


async def test_search_endpoint_finds_sheets_snippets_and_suggestions(client):
    doc = await _upload_numeric_set(client)
    r = (await client.get(f"/api/assistant/{doc}/search", params={"q": "ceiling height"})).json()
    assert r["mode"] == "all" and [x["label"] for x in r["results"]] == ["1.1a", "1.3"] or r["results"][0]["label"] in ("1.1a", "1.3")
    top = r["results"][0]
    assert top["snippets"] and top["snippets"][0]["spans"]
    jump = (await client.get(f"/api/assistant/{doc}/search", params={"q": "go to 4.0"})).json()["results"][0]
    assert jump["label"] == "4.0" and jump["kind"] == "sheet"
    typo = (await client.get(f"/api/assistant/{doc}/search", params={"q": "washrom"})).json()
    assert typo["mode"] == "none" and typo["suggestion"] == "washroom"
    assert (await client.get(f"/api/assistant/{doc}/search", params={"q": ""})).status_code == 422
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"/api/assistant/{missing}/search", params={"q": "x"})).status_code == 404


async def test_highlights_references_and_thumbnails_endpoints(client, tmp_path):
    doc = await _upload_numeric_set(client)
    pages = (await client.get(f"/api/assistant/{doc}/pages")).json()
    by_label = {p["label"]: p["page_number"] for p in pages}

    hl = (await client.get(f"/api/assistant/{doc}/pages/{by_label['1.3']}/highlights", params={"q": "ceiling"})).json()
    assert hl["boxes"] and all(0 <= b["x"] <= 1 and 0 < b["w"] < 1 for b in hl["boxes"])
    quote = (await client.get(f"/api/assistant/{doc}/pages/{by_label['1.3']}/highlights",
                              params={"phrase": "PANELF-1A-032"})).json()
    assert len(quote["boxes"]) >= 1                                              # a cited quote can be located

    refs = (await client.get(f"/api/assistant/{doc}/pages/{by_label['1.1a']}/references")).json()
    assert [r["target_label"] for r in refs] == ["1.3"] and refs[0]["target_page"] == by_label["1.3"]

    t = await client.get(f"/api/assistant/{doc}/pages/{by_label['1.3']}/thumbnail", params={"w": 300})
    assert t.status_code == 200 and t.headers["content-type"] == "image/jpeg"
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(t.content)).width <= 360                        # snapped to a cached size
    cached = list((tmp_path / "uploads" / "pages").rglob("thumb_*"))
    assert len(cached) == 1
    # The second request is served from the cache: delete the source image and it still works.
    for f in (tmp_path / "uploads" / "pages").rglob("2.jpg"):
        f.unlink()
    again = await client.get(f"/api/assistant/{doc}/pages/{by_label['1.3']}/thumbnail", params={"w": 300})
    assert again.status_code == 200 and again.content == t.content

    assert (await client.delete(f"/api/documents/{doc}")).status_code == 204
    assert list((tmp_path / "uploads" / "pages").rglob("thumb_*")) == []         # no orphaned cache files


async def test_navigation_endpoints_degrade_when_the_pdf_is_gone(client, tmp_path):
    import shutil
    doc = await _upload_numeric_set(client)
    shutil.rmtree(tmp_path / "uploads" / "pages", ignore_errors=True)
    for f in (tmp_path / "uploads").glob("*.pdf"):
        f.unlink()
    r = await client.get(f"/api/assistant/{doc}/pages/1/highlights", params={"q": "x"})
    assert r.status_code == 404 and "no longer in storage" in r.json()["detail"]
    # Text search needs only the database, so it still works.
    assert (await client.get(f"/api/assistant/{doc}/search", params={"q": "ceiling"})).status_code == 200
