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


# ── Phase 1: real-world drawing conventions ────────────────────────────────

from backend.services.drawing_metadata import detect_sheet_metadata, detect_title_block
from backend.services.retrieval_service import correct_typos, dimension_density, normalise_text
from collections import Counter

TITLE_BLOCK = "NOTES\nSYSTEM AS\nSPECIFIED\nREFLECTED CEILING PLAN\n1.3\nDESCRIPTION:\nSCALE:\nDRAWN BY:\nREVISIONS:\n1\nMAY 6/26- LAYOUT\n2\n"


def test_title_block_numeric_sheet_number_and_title():
    assert detect_title_block(TITLE_BLOCK) == ("1.3", "REFLECTED CEILING PLAN")
    assert detect_title_block("FINISH & GRAPHICS PLAN\n1.4a\nDESCRIPTION:\nSCALE:\n") == ("1.4a", "FINISH & GRAPHICS PLAN")


def test_title_block_wrapped_title():
    raw = "GENERAL REQUIREMENTS\n& SPECIFICATIONS\n0.2\nDESCRIPTION:\nSCALE:\n"
    assert detect_title_block(raw) == ("0.2", "GENERAL REQUIREMENTS & SPECIFICATIONS")


def test_title_block_label_before_value():
    assert detect_title_block("SHEET NO.\nE2.01\nSHEET TITLE\nLIGHTING PLAN\n")[0] == "E2.01"


def test_numbers_outside_a_title_block_are_not_sheet_numbers():
    body = "MOUNT AT 18\" A.F.F. 1.6\nPROVIDE 2.5 TON UNIT\nSEE 1.3 FOR DETAILS\n"
    assert detect_sheet_metadata(body, body) == (None, None)


def test_cover_sheet_gets_no_guessed_title():
    cover = "DRAWING INDEX\nWASHROOM DETAILS 4.0\nMILLWORK DETAILS 3.1\n"
    assert detect_sheet_metadata(cover, cover) == (None, None)


def test_sheet_ref_inside_sentence_is_not_the_sheet():
    text = "x\n" * 5 + "SEE DETAIL ON E2.01 FOR MORE\nLIGHTING PLAN\n"
    assert detect_sheet_number(text) is None


def test_dotted_abbreviations_and_area_phrases():
    assert "AFF" in tokenize('MOUNT 18" A.F.F. TYPICAL')
    assert "E2.01" in tokenize("SEE E2.01")  # sheet numbers untouched
    assert "BOH" in tokenize("BACK OF HOUSE: 621 SF")
    assert "BOH" in tokenize("lights in the back-of-house")


def test_numeric_sheet_reference_in_question():
    pages = [PageDoc(1, "x", "1.3", "REFLECTED CEILING PLAN"), PageDoc(2, "y", "1.1a", "EQUIPMENT PLAN")]
    assert sheet_refs_in("What is shown on sheet 1.3?", pages) == [1]
    assert sheet_refs_in("What does detail 1/1.1a show?", pages) == [2]
    assert sheet_refs_in("The unit is rated 1.3 GPF", pages) == []  # a number, not a sheet


def test_typo_correction_uses_document_vocabulary():
    vocab = Counter({"HEIGHT": 9, "WEIGHT": 2, "CEILING": 5})
    assert correct_typos({"HIEGHT": 1.0}, vocab) == {"HEIGHT": 0.9}
    assert correct_typos({"HEIGHT": 1.0}, vocab) == {}        # already a document word
    assert correct_typos({"RTU-9": 1.0}, vocab) == {}         # tags are never corrected


def test_answer_type_words_do_not_outrank_the_topic():
    spec = PageDoc(1, "VERIFY DIMENSIONS " * 12 + "general conditions", "0.1", "SPECIFICATIONS")
    wash = PageDoc(2, "WASHROOM DETAIL PLAN 10'-3\" 6'-6\" BARRIER FREE VANITY", "4.0", "WASHROOM DETAILS")
    assert rank_pages("what are the dimensions for the washroom", [spec, wash])[0].page_number == 2


def test_dimension_questions_favour_dimensioned_sheets():
    prose = PageDoc(1, "the ceiling shall be installed " * 30, "0.2", "SPECIFICATIONS")
    plan = PageDoc(2, "CEILING PLAN " + "10'-8\" 12'-7\" 5'-0\" " * 15, "1.3", "CEILING PLAN")
    assert dimension_density(plan.text) > dimension_density(prose.text)
    assert rank_pages("how tall is the ceiling", [prose, plan])[0].page_number == 2


def test_boh_matches_back_of_house_without_matching_every_house():
    area = PageDoc(1, "BACK OF HOUSE: 621 SF FRONT OF HOUSE: 384 SF", None, None)
    other = PageDoc(2, "house keeping and back charges apply to the house", None, None)
    assert rank_pages("how big is the BOH?", [area, other])[0].page_number == 1
    assert 2 not in [r.page_number for r in rank_pages("how big is the BOH?", [area, other])]
