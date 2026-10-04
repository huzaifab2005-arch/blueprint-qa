"""Search across sheets, highlights on a sheet, and cross-references."""
import pytest

from backend.services.search_service import PageRecord, parse_query, search_pages

PAGES = [
    PageRecord(1, "COVER\nDRAWING INDEX\n1.3 REFLECTED CEILING PLAN\n1.6 ELECTRICAL PLAN", None, None),
    PageRecord(2, "EQUIPMENT\nRTU-1 TRANE YHC074 7.5 TON\nREFER TO 1.3 FOR CEILING HEIGHT", "1.1a", "EQUIPMENT PLAN"),
    PageRecord(3, "NEW SUSPENDED 2'x2' GRID CEILING\n10'-8\" A.F.F. TYPICAL THROUGHOUT\nWASHROOM 8'-6\" A.F.F.\n"
                  "LIGHTING LEGEND 2'x2' RECESSED FIXTURE", "1.3", "REFLECTED CEILING PLAN"),
    PageRecord(4, "WASHROOM DETAIL PLAN\nBARRIER FREE VANITY\nDOOR 108 AND NOTE 10 8", "4.0", "WASHROOM DETAILS"),
]


def labels(resp):
    return [r.label for r in resp.results]


def test_tags_and_sizes_match_however_they_are_written():
    assert labels(search_pages(PAGES, "RTU-1")) == ["1.1a"]
    assert labels(search_pages(PAGES, "rtu1")) == ["1.1a"]
    assert labels(search_pages(PAGES, "2x2")) == ["1.3"]
    assert labels(search_pages(PAGES, "a.f.f.")) == ["1.3"]


def test_all_words_must_be_present_otherwise_partial_matches_are_labelled():
    assert labels(search_pages(PAGES, "ceiling height")) == ["1.1a"]             # only one page has both
    resp = search_pages(PAGES, "ceiling vanity")                                  # no page has both
    assert resp.mode == "partial" and set(labels(resp)) >= {"1.3", "4.0"}


def test_last_word_may_be_a_prefix_so_results_appear_while_typing():
    assert "1.3" in labels(search_pages(PAGES, "reflected ceil"))
    assert search_pages(PAGES, "ceil reflected").mode in ("none", "partial")      # only the LAST word is a prefix


def test_sheet_numbers_and_pdf_pages_jump_straight_to_the_sheet():
    r = search_pages(PAGES, "go to 1.1a")
    assert labels(r) == ["1.1a"] and r.results[0].kind == "sheet"
    r = search_pages(PAGES, "1.3")
    assert r.results[0].label == "1.3" and r.results[0].kind == "sheet"          # the sheet first, then pages that mention 1.3
    assert {x.label for x in r.results} >= {"1.3", "1.1a", "Page 1"}
    r = search_pages(PAGES, "page 4")
    assert r.results[0].label == "4.0" and r.results[0].kind == "page"
    assert search_pages(PAGES, "page 99").mode == "none"


def test_quoted_phrases_and_dimensions_are_matched_as_a_unit():
    assert labels(search_pages(PAGES, '"grid ceiling"')) == ["1.3"]
    assert labels(search_pages(PAGES, '"ceiling grid"')) == []                    # order matters
    r = search_pages(PAGES, "10'-8\"")
    assert labels(r) == ["1.3"]                                                   # not page 4, which has a 10 and an 8 apart
    assert r.results[0].snippets and "10'-8" in r.results[0].snippets[0].text


def test_snippets_say_where_the_match_is():
    r = search_pages(PAGES, "RTU-1")
    sn = r.results[0].snippets[0]
    assert sn.text.startswith("RTU-1") and [sn.text[a:b] for a, b in sn.spans] == ["RTU-1"]


def test_zero_results_offer_the_documents_own_spelling():
    r = search_pages(PAGES, "washrm")
    assert r.mode == "none" and r.suggestion == "washroom"
    assert search_pages(PAGES, "zzzzqq").suggestion is None
    assert search_pages(PAGES, "").results == []


def test_title_matches_rank_higher_and_are_flagged():
    r = search_pages(PAGES, "washroom")
    assert r.results[0].label == "4.0" and r.results[0].title_match


def test_parse_query():
    q = parse_query('what is the "grid ceiling" height?')
    assert q.phrases == ["GRIDCEILING"] and "HEIGHT" in q.terms and "WHAT" not in q.terms
    assert parse_query("10'-8\"").phrases == ["108"] and parse_query("10'-8\"").terms == []


def test_a_pdf_page_number_or_bare_digits_never_open_a_numeric_sheet():
    pages = PAGES + [PageRecord(17, "SOMETHING", "1.7", "COMMUNICATION PLAN")]
    r = search_pages(pages, "page 17")
    assert r.results[0].page_number == 17 and r.results[0].kind == "page"        # PDF page 17, not sheet 1.7 (key "17")
    assert not any(x.kind == "sheet" for x in search_pages(pages, "page 17").results)
    assert not any(x.kind == "sheet" for x in search_pages(pages, "13").results)  # "13" is not sheet 1.3
    assert search_pages(pages, "1.7").results[0].kind == "sheet"                  # typed with its dot, it is
    assert search_pages(pages, "go to 1.1a").results[0].kind == "sheet"           # a lettered suffix is fine
