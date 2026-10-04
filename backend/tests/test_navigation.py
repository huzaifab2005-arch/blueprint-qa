"""Highlights and cross-references on real (generated) PDFs, incl. a rotated page."""
import pytest

pytest.importorskip("reportlab")

from backend.evals.count_sample import build_count_pdf
from backend.evals.sample_set import build_numeric_titleblock_pdf
from backend.services import navigation_service as N
from backend.services.counting.pdf_geometry import load_geometry
from backend.services.drawing_metadata import normalise_sheet_key, page_label
from backend.services.search_service import parse_query


def _geoms(pdf_bytes, tmp_path, name):
    p = tmp_path / name
    p.write_bytes(pdf_bytes)
    return str(p)


def test_highlights_find_every_tag_and_phrase(tmp_path):
    path = _geoms(build_count_pdf(), tmp_path, "c.pdf")
    g = load_geometry(path, 1)
    q = parse_query("L1")
    assert len(N.find_highlights(g, q.terms, q.phrases)) == 8                    # eight L1 labels on the plan
    q = parse_query('"6 downlight"')
    boxes = N.find_highlights(g, q.terms, q.phrases)
    assert len(boxes) == 1 and all(0 <= b.x <= 1 and 0 <= b.y <= 1 for b in boxes)
    assert N.find_highlights(g, ["NOSUCHTHING"], []) == []


def test_highlight_boxes_sit_on_the_ink_of_a_rotated_page(tmp_path):
    from pdf2image import convert_from_path

    path = _geoms(build_count_pdf(rotate_first=270), tmp_path, "r.pdf")
    g = load_geometry(path, 1)
    boxes = N.find_highlights(g, ["L1"], [])
    assert len(boxes) == 8
    img = convert_from_path(path, dpi=72, first_page=1, last_page=1)[0].convert("L")
    assert img.width > img.height                                                  # displayed landscape

    def ink(b, dx=0.0):
        x0, y0 = int((b.x + dx) * img.width), int(b.y * img.height)
        x1, y1 = int((b.x + b.w + dx) * img.width) + 1, int((b.y + b.h) * img.height) + 1
        px = [img.getpixel((x, y)) for x in range(max(x0, 0), min(x1, img.width)) for y in range(max(y0, 0), min(y1, img.height))]
        return sum(p < 140 for p in px) / max(len(px), 1)

    assert min(ink(b) for b in boxes) > 0.08                                       # text is under every box
    assert max(ink(b, dx=-0.4) for b in boxes) < min(ink(b) for b in boxes)       # and not half a page away


def test_cross_references_need_a_cue_for_plain_numbers(tmp_path):
    path = _geoms(build_numeric_titleblock_pdf(), tmp_path, "n.pdf")
    from backend.evals.run_eval import index_pdf_offline

    pages = index_pdf_offline(path)
    sheets = {normalise_sheet_key(s): (n, page_label(n, s), t) for n, s, t, _ in pages if s}
    page_of = {s: n for n, s, _t, _x in pages}
    refs = N.find_references(load_geometry(path, page_of["1.1a"]), sheets, normalise_sheet_key("1.1a"))
    assert [r.target_label for r in refs] == ["1.3"]                               # "REFER TO 1.3" yes; "1.3 GPF" and "SEE 1.6" (no such sheet) no
    assert "REFER TO 1.3" in refs[0].text and 0 <= refs[0].box.x <= 1
    assert N.find_references(load_geometry(path, page_of["1.3"]), sheets, normalise_sheet_key("1.3")) == []   # never to itself


def test_dimension_highlights_keep_their_foot_and_inch_marks():
    from backend.services.counting.pdf_geometry import PageGeometry, Word

    words = [Word("CEILING", 10, 50, 10, 18), Word("10'-8\"", 55, 90, 10, 18),
             Word("DOOR", 10, 40, 40, 48), Word("108", 45, 65, 40, 48), Word("NOTE", 10, 40, 70, 78),
             Word("10", 45, 55, 70, 78), Word("8", 58, 65, 70, 78)]
    geom = PageGeometry(1, 200, 100, words, [])
    pq = parse_query("10'-8\"")
    boxes = N.find_highlights(geom, pq.terms, pq.phrases, patterns=pq.patterns)
    assert len(boxes) == 1 and boxes[0].text.startswith("10'-8")     # not "108", not "10 8"
