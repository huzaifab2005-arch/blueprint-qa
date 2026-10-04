"""Object counting: geometry, text evidence, confidence grading and the API path.

The synthetic drawing set has quantities known by construction (see
evals/count_sample.py). Where a number is checked on a rotated page, the markers are
verified against the RENDERED image, not just against the PDF's own coordinates.
"""
import asyncio

import pytest

pytest.importorskip("reportlab")

from backend.evals import run_eval
from backend.evals.count_sample import COUNT_CASES, build_count_pdf
from backend.services.counting import count_service as C
from backend.services.counting import symbol_counter as S
from backend.services.counting.count_service import MethodResult, PageMeta, classify, count_objects
from backend.services.counting.objects import is_count_question, parse_count_question, size_variants
from backend.services.counting.pdf_geometry import PageGeometry, Shape, Word
from backend.services.counting.tag_counter import clean_word, find_rows, tag_key


# ── Question parsing ────────────────────────────────────────────────────────

@pytest.mark.parametrize("question,cls,sizes,area", [
    ("How many 2x4 lights?", "light", ["2X4"], None),
    ("How many RTUs?", "rtu", [], None),
    ("How many diffusers?", "diffuser", [], None),
    ("How many doors?", "door", [], None),
    ("How many toilets?", "toilet", [], None),
    ("how many 2'x4' light fixtures are in BOH?", "light", ["2X4"], "BOH"),
    ("How many exhaust fans are there?", "fan", [], None),
])
def test_parse_count_question(question, cls, sizes, area):
    assert is_count_question(question)
    p = parse_count_question(question)
    assert p.object_class.key == cls and p.sizes == sizes and p.area == area


def test_unknown_object_gets_an_adhoc_class_and_non_count_is_ignored():
    p = parse_count_question("How many ducts are shown?")
    assert p.object_class is None and p.words == ["DUCT"]
    assert not is_count_question("What model is RTU-1?")
    assert not is_count_question("What is the total ceiling height?")


def test_size_variants_cover_feet_and_inches():
    assert {"2X4", "4X2", "24X48", "48X24"} <= size_variants("2X4")
    assert "2X4" in size_variants("24X48")


# ── Outline matching ────────────────────────────────────────────────────────

def _shape(kind, x0, top, w, h, pts):
    return Shape(kind, x0, x0 + w, top, top + h, tuple(pts))


def _square_diag(x, y, s=24, mirrored=False, start=0):
    corners = [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)]
    corners = corners[start:] + corners[1:start + 1] if start else corners
    diag = ((x, y + s), (x + s, y)) if mirrored else ((x, y), (x + s, y + s))
    return [_shape("curve", x, y, s, s, corners), _shape("line", x, y, s, s, diag)]


def test_mirrored_and_restarted_outlines_match():
    legend = _square_diag(0, 0, mirrored=True)[0]
    plan = _shape("curve", 100, 100, 24, 24, [(124, 124), (100, 100), (124, 100), (124, 124), (100, 124), (100, 100)])
    # a square with one diagonal, whichever way it leans and wherever the path starts
    a = _shape("curve", 0, 0, 24, 24, [(24, 0), (0, 24), (24, 24), (24, 0), (0, 0), (0, 24)])
    assert S._same_outline(a, plan)
    assert not S._same_outline(a, _shape("curve", 0, 0, 24, 24, [(0, 0), (24, 0), (24, 24), (0, 24), (0, 0)]))


def test_composite_symbol_is_counted_and_a_subshape_does_not_double_count():
    def potlight(x, y, k=1.0):
        r = 3.4 * k
        circle = _shape("curve", x - r, y - r, 2 * r, 2 * r,
                        [(x, y + r), (x + r, y), (x, y - r), (x - r, y), (x, y + r)])
        return [circle, _shape("line", x - 4.6 * k, y, 9.2 * k, 0, [(x - 4.6 * k, y), (x + 4.6 * k, y)]),
                _shape("line", x, y - 4.6 * k, 0, 9.2 * k, [(x, y - 4.6 * k), (x, y + 4.6 * k)])]

    shapes = potlight(20, 20) + potlight(200, 200, 1.3) + potlight(260, 200, 1.3) + potlight(320, 200, 1.3)
    geom = PageGeometry(1, 600, 800, [], shapes)
    template = S.Template("6in downlight", shapes[:3], 1, S._bbox(shapes[:3]))
    count = S.count_template(geom, template, {id(s) for s in shapes[:3]})
    assert len(count.instances) == 3
    assert count.uniform_scale and not count.same_size_as_swatch      # all 1.3x: a legend-vs-plan size offset
    mixed = PageGeometry(1, 600, 800, [], shapes + potlight(400, 300, 0.9)[:3])
    assert not S.count_template(mixed, template, {id(s) for s in shapes[:3]}).uniform_scale


def test_plain_swatches_and_numbered_bubbles_are_refused():
    circle = _shape("curve", 40, 40, 10, 10, [(45, 50), (50, 45), (45, 40), (40, 45), (45, 50)])
    words = [Word("PLAIN", 60, 100, 42, 50)]
    geom = PageGeometry(1, 300, 300, words, [circle])
    assert S.complexity([circle]) < S.MIN_SWATCH_EDGES
    assert S._swatch_clusters(geom, (60, 40, 100, 50)) == []        # too plain to identify by outline
    hexa = _shape("curve", 40, 80, 14, 12, [(40, 86), (44, 80), (50, 80), (54, 86), (50, 92), (44, 92), (40, 86)])
    bubble = PageGeometry(1, 300, 300, [Word("5", 45, 49, 83, 89), Word("NOTE", 60, 90, 83, 91)], [hexa])
    assert S._swatch_clusters(bubble, (60, 80, 90, 92)) == []        # has a number inside: a keynote bubble


def test_callout_bubble_columns_are_detected():
    insts = [S.Instance(40, 100 + i * 30, 54, 112 + i * 30, 1.0) for i in range(4)]
    words = [Word(w, 60 + 14 * k, 72 + 14 * k, 102 + i * 30, 110 + i * 30)
             for i in range(4) for k, w in enumerate(["SUPPLY", "AND", "INSTALL"])]
    geom = PageGeometry(1, 300, 400, words, [])
    assert S.looks_like_callout_bubbles(geom, insts)
    assert not S.looks_like_callout_bubbles(PageGeometry(1, 300, 400, [], []), insts)


# ── Confidence grading ──────────────────────────────────────────────────────

def _m(method, q):
    return MethodResult(method, q, {1: q}, {}, f"{method} {q}")


def test_classify_truth_table():
    assert classify([], [])[0] == C.NOT_FOUND
    assert classify([_m("symbol", 8)], [])[0] == C.SINGLE_SOURCE
    assert classify([_m("symbol", 8), _m("schedule_qty", 8)], [])[0] == C.CROSS_CHECKED
    status, primary, notes = classify([_m("symbol", 8), _m("schedule_qty", 9)], [])
    assert status == C.NEEDS_VERIFICATION and primary.method == "symbol" and "disagree" in notes[0]
    assert classify([_m("symbol", 8), _m("schedule_qty", 8)], ["area not applied"])[0] == C.NEEDS_VERIFICATION
    assert classify([_m("vision", 5)], [])[0] == C.NEEDS_VERIFICATION      # a vision estimate is never better


def test_vision_agreement_never_upgrades_a_count():
    # A vision estimate that agrees with ONE deterministic source is still one source.
    status, primary, _ = classify([_m("vision", 8), _m("schedule_qty", 8)], [])
    assert status == C.SINGLE_SOURCE and primary.method == "schedule_qty"


# ── Text evidence ───────────────────────────────────────────────────────────

def _words(rows):
    out = []
    for y, cells in rows:
        for x, text in cells:
            out.append(Word(text, x, x + 6 * len(text), y, y + 8))
    return out


def test_schedule_rows_qty_column_and_size_filter():
    words = _words([
        (100, [(60, "TYPE"), (120, "DESCRIPTION"), (400, "QTY")]),
        (120, [(60, "L1"), (120, "2X4"), (160, "LED"), (190, "TROFFER"), (402, "14")]),
        (140, [(60, "L2"), (120, "6IN"), (160, "LED"), (190, "DOWNLIGHT"), (402, "3")]),
    ])
    geom = PageGeometry(1, 600, 800, words, [])
    parsed = parse_count_question("How many 2x4 lights?")
    rows, wrong = find_rows(geom, parsed.object_class, parsed)
    assert [(r.tag, r.qty) for r in rows] == [("L1", 14)]
    assert wrong and wrong[0].startswith("L2")
    parsed = parse_count_question("How many downlights?")
    rows, _ = find_rows(geom, parsed.object_class, parsed)
    assert [r.tag for r in rows] == ["L2"]             # "LED" in the L1 row must not satisfy "downlight"


def test_tag_normalisation():
    assert tag_key("RTU-1") == tag_key("rtu1") == "RTU1"
    assert clean_word("(L1),") == "L1"


# ── The engine against ground truth ─────────────────────────────────────────

def _run(pdf_bytes, tmp_path, question):
    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(pdf_bytes)
    pages = run_eval.index_pdf_offline(str(pdf))
    metas = [PageMeta(n, s, t, tx) for n, s, t, tx in pages]
    return asyncio.run(count_objects(question, parse_count_question(question), metas, str(pdf)))


@pytest.mark.parametrize("case", COUNT_CASES, ids=[c["id"] for c in COUNT_CASES])
def test_ground_truth_cases(case, tmp_path):
    out = _run(build_count_pdf(), tmp_path, case["question"])
    assert out.quantity == case["expected_count"], (out.status, [(m.method, m.quantity) for m in out.methods])
    assert out.status in case["expected_status"], (out.status, out.blocking)


def test_three_independent_methods_agree_for_troffers(tmp_path):
    out = _run(build_count_pdf(), tmp_path, "How many 2x4 lights?")
    assert {m.method for m in out.methods} == {"tag_instances", "symbol", "schedule_qty"}
    assert out.status == C.CROSS_CHECKED and out.confidence == "high"
    assert len(out.methods[0].markers) == 8


def test_schedule_and_plan_disagree_so_it_needs_verification(tmp_path):
    out = _run(build_count_pdf(schedule_qty_l1=9), tmp_path, "How many 2x4 lights?")
    assert out.status == C.NEEDS_VERIFICATION and out.quantity == 8
    assert any("disagree" in w for w in out.warnings)
    assert "needs verification" in out.summary


def test_an_unlabelled_missing_fixture_is_caught_by_the_schedule(tmp_path):
    out = _run(build_count_pdf(l1_on_plan=7), tmp_path, "How many 2x4 lights?")   # 7 drawn, schedule says 8
    assert out.status == C.NEEDS_VERIFICATION and {m.quantity for m in out.methods} == {7, 8}


def test_size_not_in_the_set_is_not_answered_with_another_size(tmp_path):
    out = _run(build_count_pdf(), tmp_path, "How many 2x2 lights?")
    assert out.status == C.NOT_FOUND and out.quantity is None
    assert any("not at the size" in w for w in out.warnings)


def test_door_style_tag_caveat_blocks_a_verified_status(tmp_path):
    cls = parse_count_question("How many doors?").object_class
    assert cls.tag_caveat and "TYPES" in cls.tag_caveat


def test_rotated_page_counts_the_same_and_markers_sit_on_the_symbols(tmp_path):
    from pdf2image import convert_from_path

    pdf_bytes = build_count_pdf(rotate_first=270)
    out = _run(pdf_bytes, tmp_path, "How many 2x4 lights?")
    assert out.quantity == 8
    markers = [m for m in out.methods[0].markers if m.page_number == 1]
    assert len(markers) == 8
    img = convert_from_path(str(tmp_path / "c.pdf"), dpi=72, first_page=1, last_page=1)[0].convert("L")
    assert img.width > img.height                       # displayed landscape: the rotation is applied

    def ink(m, dx=0.0, dy=0.0):
        x0, y0 = int((m.x + dx) * img.width), int((m.y + dy) * img.height)
        x1, y1 = int((m.x + m.w + dx) * img.width) + 1, int((m.y + m.h + dy) * img.height) + 1
        px = [img.getpixel((x, y)) for x in range(max(x0, 0), min(x1, img.width)) for y in range(max(y0, 0), min(y1, img.height))]
        return sum(p < 140 for p in px) / max(len(px), 1)

    on = [ink(m) for m in markers]
    off = [ink(m, dx=-0.5) for m in markers]           # the same boxes moved half a page away
    assert min(on) > 0.05 and max(off) < min(on)


# ── Vision fallback ─────────────────────────────────────────────────────────

def test_vision_estimate_is_only_used_when_nothing_else_identifies_the_object(tmp_path, monkeypatch):
    from backend.services import assistant_service

    calls = []

    async def fake(model, messages, max_tokens=1200):
        calls.append(messages)
        return {"count": 4, "confidence": "high", "note": "counted four"}

    monkeypatch.setattr(assistant_service, "_chat_json", fake)
    monkeypatch.setattr(assistant_service, "_load_model_image_b64", lambda path: "AAAA")
    from backend.config import get_settings
    monkeypatch.setattr(get_settings(), "nvidia_api_key", "nvapi-test")

    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(build_count_pdf())
    pages = run_eval.index_pdf_offline(str(pdf))
    metas = [PageMeta(n, s, t, tx, "img.jpg") for n, s, t, tx in pages]
    q = "How many ducts are shown?"                    # "DUCT" is on the roof plan but has no tag, row or legend
    out = asyncio.run(count_objects(q, parse_count_question(q), metas, str(pdf)))
    assert out.status == C.NEEDS_VERIFICATION and out.primary == "vision" and out.quantity == 4
    assert any("vision-model estimate" in w for w in out.warnings)
    assert calls                                        # it did ask the model

    calls.clear()
    out = asyncio.run(count_objects("How many 2x4 lights?", parse_count_question("How many 2x4 lights?"), metas, str(pdf)))
    assert out.primary != "vision" and not calls         # deterministic evidence exists: the model is not asked


def test_vision_is_not_asked_to_count_something_the_sheets_never_mention(tmp_path, monkeypatch):
    """A vision model asked to count what is not there tends to invent a number."""
    from backend.services import assistant_service
    from backend.config import get_settings

    calls = []

    async def fake(model, messages, max_tokens=1200):
        calls.append(1)
        return {"count": 3, "confidence": "high", "note": "three"}

    monkeypatch.setattr(assistant_service, "_chat_json", fake)
    monkeypatch.setattr(assistant_service, "_load_model_image_b64", lambda path: "AAAA")
    monkeypatch.setattr(get_settings(), "nvidia_api_key", "nvapi-test")
    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(build_count_pdf())
    pages = run_eval.index_pdf_offline(str(pdf))
    metas = [PageMeta(n, s, t, tx, "img.jpg") for n, s, t, tx in pages]
    for q in ("How many urinals?", "How many sprinklers are shown?"):
        out = asyncio.run(count_objects(q, parse_count_question(q), metas, str(pdf)))
        assert out.status == C.NOT_FOUND and out.quantity is None
    assert not calls


def test_a_superset_symbol_is_not_counted_as_the_simpler_one_even_when_not_asked_about(tmp_path):
    """Pendants contain the downlight symbol. Asking only about downlights must not
    count the pendants or the pendant's own legend swatch (found on a real set: 'recessed
    fixtures' returned 20 instead of 17 until every legend entry took part in the matching)."""
    pdf = build_count_pdf(pendants=2)
    out = _run(pdf, tmp_path, "How many downlights?")
    symbol = next(m for m in out.methods if m.method == "symbol")
    assert symbol.quantity == 3, symbol.detail
    pend = _run(pdf, tmp_path, "How many pendant lights?")
    assert next(m for m in pend.methods if m.method == "symbol").quantity == 2
