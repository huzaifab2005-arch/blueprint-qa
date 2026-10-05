from backend.services.rules import check_references, check_spec_conflicts

SHEET = """SHEET INDEX:
A-101 FLOOR PLAN
A-501 DETAILS
M-101 HVAC PLAN
E-101 POWER PLAN
SECTION A-A SECTION B-B DETAIL 1 - CURB DETAIL 2 - PIPE SUPPORT
SHEET NO: A-101
"""


def notes(*lines):
    return SHEET + "GENERAL NOTES:\n" + "\n".join(f"{i}. {l}" for i, l in enumerate(lines, 1))


def descs(issues):
    return [i["description"] for i in issues]


# ---- references: true positives
def test_missing_detail():
    out = check_references(notes("SEE DETAIL 5 FOR CURB CONDITION."))
    assert len(out) == 1 and "DETAIL 5" in out[0]["description"]


def test_missing_section():
    out = check_references(notes("SEE SECTION C-C FOR WALL ASSEMBLY."))
    assert len(out) == 1 and "SECTION C-C" in out[0]["description"]


def test_invalid_sheet():
    out = check_references(notes("REFER TO SHEET M-901 FOR HVAC LAYOUT."))
    assert len(out) == 1 and "M-901" in out[0]["description"]


# ---- references: must stay quiet
def test_valid_references_are_clean():
    assert check_references(notes(
        "SEE DETAIL 1 FOR CURB.", "SEE SECTION A-A FOR WALL.", "REFER TO SHEET M-101 FOR HVAC.")) == []


def test_detail_qualified_to_other_sheet_is_not_flagged():
    assert check_references(notes("SEE DETAIL 9/A-501 AT DOOR HEAD.")) == []


def test_no_local_context_means_no_detail_verdict():
    # No details/sections defined on the page: the reference may live on another sheet.
    text = "GENERAL NOTES:\n1. SEE DETAIL 5 FOR CURB.\n2. SEE SECTION C-C FOR WALL."
    assert check_references(text) == []


def test_sheet_reference_needs_an_index():
    assert check_references("1. REFER TO SHEET M-901 FOR HVAC.\nA-101 FLOOR PLAN") == []


def test_ocr_noise_in_sheet_id_is_tolerated():
    assert check_references(notes("REFER TO SHEET M-1O1 FOR HVAC.")) == []  # O vs 0


def test_equipment_tag_is_not_a_sheet_reference():
    assert check_references(notes("SEE D-107 FOR DOOR.")) == []


# ---- spec conflicts
def test_wall_spec_conflict():
    out = check_spec_conflicts(notes('WALLS TO ROOM 204 ARE 8" CMU.', 'ROOM 204 WALLS ARE 6" GYP STUD.'))
    assert len(out) == 1
    d = out[0]["description"]
    assert "CMU" in d and "GYP" in d and "ROOM 204" in d


def test_thickness_conflict_same_material():
    out = check_spec_conflicts(notes('ALL WALLS ARE 8" CMU.', 'ALL WALLS ARE 6" CMU.'))
    assert len(out) == 1


def test_consistent_specs_are_clean():
    assert check_spec_conflicts(notes('WALLS TO ROOM 204 ARE 8" CMU.', 'ROOM 204 WALLS ARE 8" CMU.')) == []


def test_different_elements_or_scopes_are_not_conflicts():
    assert check_spec_conflicts(notes(
        'WALLS TO ROOM 204 ARE 8" CMU.', 'WALLS TO ROOM 301 ARE 6" GYP STUD.',
        'PARTITIONS TO ROOM 204 ARE 6" GYP STUD.')) == []


def test_unscoped_statements_are_ignored():
    assert check_spec_conflicts(notes('WALLS ARE 8" CMU.', 'WALLS ARE 6" GYP STUD.')) == []


def test_empty_text():
    assert check_references("") == [] and check_spec_conflicts("") == []
