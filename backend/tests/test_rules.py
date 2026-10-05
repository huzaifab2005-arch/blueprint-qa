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


# ---- missing scale
from backend.services.rules import check_missing_scale, check_tags, schedule_rows

PLAN_NO_SCALE = """MECHANICAL ROOM PLAN
40'-0"
40'-0"
25'-0"
GENERAL NOTES:
1. DO NOT SCALE DRAWINGS.
"""


def test_missing_scale_flagged_on_plan_with_dimensions():
    out = check_missing_scale(PLAN_NO_SCALE)
    assert len(out) == 1 and out[0]["issue_type"] == "missing_scale"


def test_scale_note_or_nts_is_clean():
    assert check_missing_scale(PLAN_NO_SCALE + 'SCALE: 1/4" = 1\'-0"') == []
    assert check_missing_scale(PLAN_NO_SCALE + "NTS") == []
    assert check_missing_scale(PLAN_NO_SCALE + "1/8\" = 1'-0\"") == []


def test_schedule_or_notes_sheet_without_scale_is_not_flagged():
    assert check_missing_scale("DOOR SCHEDULE\nD-101 3'-0\" x 7'-0\"\nD-102 3'-0\" x 7'-0\"") == []  # no view title
    assert check_missing_scale("FLOOR PLAN NOTES\n1. VERIFY ON SITE.") == []                          # no dimensions


# ---- tag checks (synthetic word boxes)
def W(text, x, y, line, w=60, h=16):
    return {"text": text, "left": x, "top": y, "width": w, "height": h, "block": 1, "par": 1, "line": line}


def sched_rows(tags, y0=400, base_line=100):
    out = []
    for i, t in enumerate(tags):
        ln = base_line + i
        out += [W(t, 1450, y0 + 34 * i, ln), W("AIR", 1540, y0 + 34 * i, ln), W("HANDLING", 1610, y0 + 34 * i, ln, w=110),
                W("UNIT", 1730, y0 + 34 * i, ln)]
    return out


def plan_labels(tags_xy):
    return [W(t, x, y, 500 + i) for i, (t, x, y) in enumerate(tags_xy)]


def test_schedule_rows_found_from_default_layout_words():
    assert set(schedule_rows(sched_rows(["AHU-1", "AHU-2"]))) == {"AHU1", "AHU2"}


def test_schedule_tag_missing_from_plan():
    words = sched_rows(["AHU-1", "AHU-2", "AHU-3"])
    plan = words + plan_labels([("AHU-1", 480, 550), ("AHU-2", 800, 550)])
    out = check_tags(words, plan)
    assert [i["description"] for i in out] == ["AHU-3 is listed in the schedule but is not tagged on the plan."]


def test_duplicate_equipment_tag_on_plan():
    words = sched_rows(["AHU-1", "AHU-2"])
    plan = words + plan_labels([("AHU-1", 480, 550), ("AHU-2", 800, 550), ("AHU-2", 1000, 750)])
    out = check_tags(words, plan)
    assert len(out) == 1 and "AHU-2 appears 2 times" in out[0]["description"]


def test_plan_tag_not_in_schedule():
    words = sched_rows(["D-101", "D-102"])
    plan = words + plan_labels([("D-101", 410, 640), ("D-102", 1215, 700), ("D-107", 800, 800)])
    out = check_tags(words, plan)
    assert len(out) == 1 and out[0]["description"].startswith("D-107 is tagged on the plan")


def test_clean_page_and_repeated_door_tags_are_quiet():
    words = sched_rows(["D-101", "D-102"])
    plan = words + plan_labels([("D-101", 410, 640), ("D-101", 900, 640), ("D-102", 1215, 700)])
    assert check_tags(words, plan) == []         # door tags may repeat; only equipment tags must be unique


def test_tags_inside_notes_text_are_not_plan_labels():
    words = sched_rows(["AHU-1", "AHU-2"]) + [W("SEE", 100, 900, 700), W("AHU-2", 160, 900, 700), W("FOR", 230, 900, 700)]
    plan = words + plan_labels([("AHU-1", 480, 550), ("AHU-2", 800, 550)]) + [W("AHU-2", 160, 900, 800)]
    assert check_tags(words, plan) == []


def test_other_sheet_schedule_with_no_plan_labels_is_quiet():
    words = sched_rows(["AHU-1", "AHU-2", "AHU-3"])
    assert check_tags(words, words) == []        # plan is on another sheet


def test_stray_tag_like_words_do_not_form_a_schedule():
    # CAT6 / A-46 start text lines but are not a schedule family: nothing may be flagged.
    words = sched_rows(["CAT6", "A-46"])
    plan = words + plan_labels([("EF-3", 480, 550), ("EF-3", 900, 700)])
    assert check_tags(words, plan) == []


def test_duplicate_requires_tag_to_be_scheduled():
    words = sched_rows(["AHU-1", "AHU-2"])
    plan = words + plan_labels([("AHU-1", 480, 550), ("AHU-2", 800, 550), ("EF-3", 100, 700), ("EF-3", 900, 700)])
    assert check_tags(words, plan) == []


def test_door_schedule_rows_with_only_dimensions_count_as_a_schedule():
    rows = []
    for i, t in enumerate(["D-101", "D-102"]):
        y = 560 + 34 * i
        rows += [W(t, 1450, y, 300 + i), W("3'-0\"", 1540, y, 300 + i), W("x", 1620, y, 300 + i, w=14), W("7'-0\"", 1650, y, 300 + i)]
    assert set(schedule_rows(rows)) == {"D101", "D102"}
