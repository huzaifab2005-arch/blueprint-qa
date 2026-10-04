from backend.services.drawing_metadata import (
    detect_sheet_number, detect_sheet_title, format_sheet_number, normalise_sheet_key,
)
from backend.services.retrieval_service import (
    PageDoc, query_terms, rank_pages, select_relevant_text, sheet_refs_in, tokenize,
)
from backend.services.assistant_service import confirm_quote, parse_json_object


def test_sheet_number_labelled():
    assert detect_sheet_number("PROJECT X\nSHEET NO: E2.01\nDATE 2024") == "E2.01"
    assert detect_sheet_number("SHEET NUMBER\nM2.02\n") == "M2.02"


def test_sheet_number_from_tail_and_hyphen_form():
    body = "\n".join(["RTU-1 ROOFTOP UNIT 5 TON"] * 40)
    assert detect_sheet_number(body + "\nLIGHTING PLAN\nE-2.01") == "E2.01"
    assert detect_sheet_number(body + "\nFIRST FLOOR PLAN\nA-101") == "A-101"


def test_sheet_number_ignores_equipment_tags():
    assert detect_sheet_number("RTU-1\nAHU-2\nEF-3") is None
    assert detect_sheet_number("") is None


def test_sheet_title():
    text = "NOTES\n" + "x\n" * 5 + "LIGHTING SCHEDULE\nE2.01"
    assert detect_sheet_title(text, "E2.01") == "LIGHTING SCHEDULE"


def test_sheet_key_normalisation():
    assert normalise_sheet_key("e-2.01") == normalise_sheet_key("E2.01") == "E201"
    assert format_sheet_number("E-2.01") == "E2.01"


def test_tokenizer_keeps_tags_and_sizes():
    toks = tokenize("Provide RTU-1 with 2'x4' fixtures, see 3/M2.02")
    assert "RTU-1" in toks and "RTU" in toks
    assert "2X4" in toks
    assert "M2.02" in toks


def _pages():
    return [
        PageDoc(1, "COVER SHEET index of drawings", "G0.01", "COVER SHEET"),
        PageDoc(2, "LIGHTING SCHEDULE type L1 2X4 LED troffer model 24LED-4000 BOH storage", "E2.01", "LIGHTING SCHEDULE"),
        PageDoc(3, "MECHANICAL PLAN RTU-1 RTU-2 supply duct 24x12 ROOFTOP UNIT", "M2.01", "MECHANICAL PLAN"),
        PageDoc(4, "MECHANICAL DETAILS detail 3 RTU curb RTU-1", "M2.02", "MECHANICAL DETAILS"),
        PageDoc(5, "ELECTRICAL POWER PLAN panel LP-1 receptacles", "E3.01", "POWER PLAN"),
    ]


def test_rank_schedule_by_title():
    top = rank_pages("Which page contains the lighting schedule?", _pages())
    assert top[0].page_number == 2


def test_rank_equipment_tag():
    ranked = [r.page_number for r in rank_pages("Where is RTU-1 located?", _pages())]
    assert set(ranked[:2]) == {3, 4}
    assert 5 not in ranked and 1 not in ranked


def test_rank_synonyms_and_size_token():
    ranked = [r.page_number for r in rank_pages("How many 2x4 light fixtures are there?", _pages())]
    assert ranked[0] == 2


def test_detail_reference_pins_page():
    pages = _pages()
    assert sheet_refs_in("What does detail 3/M2.02 show?", pages) == [4]
    assert rank_pages("What does detail 3/M2.02 show?", pages)[0].page_number == 4


def test_equipment_tag_is_not_mistaken_for_a_sheet():
    assert sheet_refs_in("What model is RTU-1?", _pages()) == []


def test_no_match_returns_nothing():
    assert rank_pages("what colour is the elephant", _pages()) == []


def test_query_terms_weights():
    w = query_terms("model of RTU-1")
    assert w["RTU-1"] > w["RTU"] > 0
    assert "OF" not in w


def test_select_relevant_text_keeps_matching_lines():
    lines = [f"filler line {i}" for i in range(500)]
    lines[250] = "RTU-1 TRANE YHC074 7.5 TON"
    out = select_relevant_text("\n".join(lines), "model of RTU-1", 600)
    assert "TRANE YHC074" in out and len(out) <= 800


def test_confirm_quote():
    assert confirm_quote("RTU-1  TRANE", "xx RTU-1\n   TRANE yy")
    assert not confirm_quote("CARRIER 50XC", "RTU-1 TRANE")


def test_parse_json_object():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": 1} done') == {"a": 1}
    assert parse_json_object("nope") is None
    assert parse_json_object("[1,2]") is None
