from backend.services.evidence import Corpus, reject_reason, validate_findings

OCR = """SHEET INDEX:
A-101 FLOOR PLAN
M-101 HVAC PLAN
SECTION A-A DETAIL 1 - CURB DETAIL 2 - PIPE SUPPORT
40'-0"
36'-0"
EQUIPMENT SCHEDULE
AHU-1 AIR HANDLING UNIT 2000
GENERAL NOTES:
1. SEE DETAIL 5/M-501 FOR CURB.
2. WALLS TO ROOM 204 ARE 8" CMU.
3. SEE SECTION B-B FOR PIPING.
"""
C = Corpus(OCR)


def f(issue_type, description):
    return {"issue_type": issue_type, "severity": "low", "description": description, "location_hint": ""}


def keep(issue_type, description):
    assert reject_reason(f(issue_type, description), C) is None, description


def drop(issue_type, description, why=""):
    reason = reject_reason(f(issue_type, description), C)
    assert reason is not None and why in reason, (description, reason)


# ---- kept: grounded or genuinely visual
def test_visual_unlabeled_element_is_kept():
    keep("unlabeled_element", "The circle symbol in the center of the room is not labeled.")
    keep("missing_tag", "Three equipment boxes in the room have no tag.")


def test_citing_text_that_exists_is_kept():
    keep("dimension_mismatch", "Wall dimensions 40'-0\" and 36'-0\" conflict.")
    keep("incomplete_detail", "SECTION B-B is referenced but not shown.")   # only referenced, never titled
    keep("incomplete_detail", "Note 'DETAIL 5/M-501' is unclear.")        # quote mark must not read as 501 feet


# ---- dropped: hallucinated evidence
def test_invented_identifiers_are_dropped():
    drop("incomplete_detail", "DETAIL 9 is referenced but not shown.", "does not appear")
    drop("incomplete_detail", "SECTION C-C is referenced but not shown.", "does not appear")
    drop("missing_tag", "AHU-7 is not tagged.", "does not appear")
    drop("dimension_mismatch", "The wall is 38'-0\" but should be 40'-0\".", "does not appear")


# ---- dropped: contradicted by the sheet's own text
def test_label_claim_contradicted_by_printed_text():
    drop("missing_tag", "The HVAC plan is not labeled.", "printed on the sheet")
    drop("unlabeled_element", "The pipe support is not labeled.", "printed on the sheet")
    drop("missing_tag", "The AHU-1 is not labeled with a tag or number.", "printed on the sheet")


# ---- dropped: uncheckable / vague
def test_text_checkable_claims_need_a_citation():
    drop("dimension_mismatch", "The dimensions of the air handling units are different.", "without citing")
    drop("inconsistent_annotation", "The general notes have inconsistent dimensions.", "without citing")
    drop("missing_tag", "The HVAC layout is referenced in the general notes, but the plan is not shown on this page.",
         "without citing")


def test_vague_label_findings_are_dropped():
    drop("unlabeled_element", "There is an unlabeled element in the room.", "which element")


def test_word_boxes_extend_the_corpus():
    words = [{"text": "AHU-3", "left": 0, "top": 0, "width": 10, "height": 10, "block": 1, "par": 1, "line": 1}]
    assert reject_reason(f("missing_tag", "AHU-3 is not tagged."), C) is not None
    assert reject_reason(f("missing_tag", "AHU-3 is not tagged."), Corpus(OCR, words)) is None


def test_validate_findings_splits_kept_and_dropped():
    kept, dropped = validate_findings(
        [f("unlabeled_element", "The circle symbol is not labeled."), f("missing_tag", "The HVAC plan is not labeled.")], C)
    assert len(kept) == 1 and len(dropped) == 1 and "HVAC" in dropped[0][0]["description"]


# ---- detail/section claims contradicted by a title on the sheet
def test_detail_claimed_not_shown_but_titled_on_sheet():
    drop("incomplete_detail", "Detail 1 - Curb is not shown on this page.", "titles it")
    drop("incomplete_detail", "SECTION A-A is missing from this sheet.", "titles it")


def test_detail_only_referenced_is_still_reportable():
    keep("incomplete_detail", "DETAIL 5/M-501 is referenced but not shown.")   # 'DETAIL 5' only appears in a SEE note


def test_compound_subject_all_printed():
    drop("unlabeled_element", "The detail 1 - curb and detail 2 - pipe support are not labeled.", "printed on the sheet")


def test_compound_subject_with_unprinted_part_is_kept():
    keep("unlabeled_element", "The circle symbol and the pipe support are not labeled.")


def test_dimension_mismatch_needs_two_different_values():
    drop("dimension_mismatch", "40'-0\" is given twice.", "two different dimensions")
    drop("dimension_mismatch", "The 40'-0\" wall does not match the drawing scale.", "two different dimensions")
    keep("dimension_mismatch", "The top wall is 40'-0\" but the bottom wall is 36'-0\".")


def test_scale_based_dimension_claims_are_dropped():
    with_scale = Corpus(OCR + "\nSCALE: 1/4\" = 1'-0\"")
    reason = reject_reason(f("dimension_mismatch", "The walls are given as 8\" CMU, but the scale is 1/4\" = 1'-0\""), with_scale)
    assert reason is not None and "scale" in reason


def test_compound_subject_with_preposition_and_not_shown_in_other_types():
    drop("unlabeled_element", "The symbols for Section A-A and Section B-B are not labeled", "printed on the sheet")
    drop("inconsistent_annotation",
         "The note 'SEE DETAIL 1 FOR CURB' refers to Detail 1 - CURB, but Detail 1 - CURB is not shown on this page.",
         "titles it")
